---
title: Spark ShuffleWriter 原理
date: 2019-01-26 22:58:19
tags: spark, shuffle, writer
categories: spark
---

## Spark ShuffleWriter 原理

Spark的shuffle过程比较复杂，涉及到map端和reduce端的共同配合，这篇文章主要介绍map端的运行原理。map端的操作主要由ShuffleWriter实现，它对于不同的情形，会选用不同的算法。

## shuffle 算法选择

spark 根据不同的情形，提供三种shuffle writer选择。

- BypassMergeSortShuffleWriter ： 当前shuffle没有聚合， 并且分区数小于spark.shuffle.sort.bypassMergeThreshold（默认200）
- UnsafeShuffleWriter ： 当前rdd的数据支持序列化（即UnsafeRowSerializer），并且没有聚合， 并且分区数小于  2^24。
- SortShuffleWriter ： 其余



下图是相关的uml图

{% plantuml %}

@startuml

abstract class ShuffleHandle

class  BaseShuffleHandle

class BypassMergeSortShuffleHandle

class SerializedShuffleHandle

ShuffleHandle <|-- BaseShuffleHandle
BaseShuffleHandle <|-- BypassMergeSortShuffleHandle
BaseShuffleHandle <|-- SerializedShuffleHandle

interface ShuffleManager

class SortShuffleManager

ShuffleManager <|-- SortShuffleManager

abstract class ShuffleWriter

class BypassMergeSortShuffleWriter

class SortShuffleWriter

class UnsafeShuffleWriter

ShuffleWriter <|-- BypassMergeSortShuffleWriter

ShuffleWriter <|-- SortShuffleWriter

ShuffleWriter <|-- UnsafeShuffleWriter

interface ShuffleReader

class BlockStoreShuffleReader

ShuffleReader <|-- BlockStoreShuffleReader

ShuffleManager --> ShuffleHandle : registerShuffle

ShuffleManager --> ShuffleWriter : getWriter

ShuffleManager --> ShuffleReader : getReader

@enduml

{% endplantuml %}



ShuffleHandle类 会保存shuffle writer算法需要的信息。根据ShuffleHandle的类型，来选择ShuffleWriter的类型。

ShuffleWriter负责在map端生成中间数据，ShuffleReader负责在reduce端读取和整合中间数据。

ShuffleManager 提供了registerShuffle方法，根据shuffle的dependency情况，选择出哪种ShuffleHandler。它对于不同的ShuffleHandler，有着不同的条件

- BypassMergeSortShuffleHandle :  该shuffle不需要聚合，并且reduce端的分区数目小于配置项spark.shuffle.sort.bypassMergeThreshold，默认为200
- SerializedShuffleHandle  :  该shuffle支持数据不需要聚合，并且必须支持序列化时seek位置，还需要reduce端的分区数目小于16777216（1 << 24 + 1）
- BaseShuffleHandle  :  其余情况

getWriter方法会根据registerShuffle方法返回的ShuffleHandler，选择出哪种 shuffle writer，原理比较简单：

* 如果是BypassMergeSortShuffleHandle， 则选择BypassMergeSortShuffleWriter

* 如果是SerializedShuffleHandle， 则选择UnsafeShuffleWriter

* 如果是BaseShuffleHandle， 则选择SortShuffleWriter



ShuffleWriter只有两个方法，write和stop方法。使用者首先调用write方法，添加数据，完成排序，最后调用stop方法，返回MapStatus结果。下面依次介绍ShuffleWriter的三个子类。



## DiskBlockObjectWriter 原理

在介绍shuffle writer 之前，需要先介绍下DiskBlockObjectWriter原理，因为后面的shuffle writer 都会使用它将数据写入文件。

它提供了文件写入功能，在此之上还加入了统计，压缩和序列化。  它使用了装饰流，依次涉及FileOutputStream ， TimeTrackingOutputStream， ManualCloseBufferedOutputStream， 压缩流， 序列化流。

TimeTrackingOutputStream增加对写花费时间的统计。

ManualCloseBufferedOutputStream 继承 OutputStream， 更改了close方法。使用者必须调用manualClose方法手动关闭。这样做是防止外层的装饰流调用close，导致里面的流也会调用close。代码如下:

```scala
trait ManualCloseOutputStream extends OutputStream {
  abstract override def close(): Unit = {
    flush()
  }

  def manualClose(): Unit = {
    super.close()
  }
}
```

这里使用ManualCloseBufferedOutputStream，是因为外层的压缩流和序列化流会经常关闭和新建，所以需要保护底层的FileOutputStream 不受影响。

压缩流和序列化流都是Spark SerializerManager实例化的。

DiskBlockObjectWriter的流初始化，代码如下：

```scala
private def initialize(): Unit = {
  // 文件输出流
  fos = new FileOutputStream(file, true)
  // 获取该文件的Channel，通过Channel获取写位置
  channel = fos.getChannel()
  // 装饰流 TimeTrackingOutputStream， writeMetrics是作为统计使用的
  ts = new TimeTrackingOutputStream(writeMetrics, fos)
  // 继承缓冲流，但是作为ManualCloseOutputStream的接口
  class ManualCloseBufferedOutputStream
    extends BufferedOutputStream(ts, bufferSize) with ManualCloseOutputStream
  mcs = new ManualCloseBufferedOutputStream
}

def open(): DiskBlockObjectWriter = {
  if (hasBeenClosed) {
    throw new IllegalStateException("Writer already closed. Cannot be reopened.")
  }
  if (!initialized) {
    initialize()
    initialized = true
  }
  // 通过SerializerManager装饰压缩流
  bs = serializerManager.wrapStream(blockId, mcs)
  // 通过SerializerInstance装饰序列流
  objOut = serializerInstance.serializeStream(bs)
  streamOpen = true
  this
}
```

注意到 initialize方法只会调用一次，open方法会多次调用。因为DiskBlockObjectWriter涉及到了序列化，而序列化流是有缓存的，当每次flush序列化流后，都会关闭它，并且调用open获取新的序列化流。

DiskBlockObjectWriter提供了write方法写数据，还提供了commitAndGet方法flush序列化流。commitAndGet返回FileSegment，包含了自从上一次提交开始，到此次commit的写入数据的位置信息 (起始位置，数据长度)。

```scala
def write(key: Any, value: Any) {
  if (!streamOpen) {
    open()
  }
  // 只是简单调用了objOut流，写入key和value
  objOut.writeKey(key)
  objOut.writeValue(value)
  // 记录写入的数据条数和字节数
  recordWritten()
}

def commitAndGet(): FileSegment = {
    if (streamOpen) {
        // NOTE: Because Kryo doesn't flush the underlying stream we explicitly flush both the
        //       serializer stream and the lower level stream.
        // 调用objOut流的flush
        objOut.flush()
        // 调用bs的flush
        bs.flush()
        // 关闭objOut流
        objOut.close()
        streamOpen = false

        if (syncWrites) {
            // 调用文件sync方法，强制flush内核缓存
            val start = System.nanoTime()
            fos.getFD.sync()
            writeMetrics.incWriteTime(System.nanoTime() - start)
        }
        // 获取文件的写位置
        val pos = channel.position()
        // committedPosition表示上一次commit的时候的位置
        // 如果是第一次commit，那么是打开文件时的长度
        // pos - committedPosition计算出自从上一次commit开始，写入数据的长度
        val fileSegment = new FileSegment(file, committedPosition, pos - committedPosition)
        // 更新 committedPosition
        committedPosition = pos
        // 更新写入字节数
        writeMetrics.incBytesWritten(committedPosition - reportedPosition)
        fileSegment
    } else {
        new FileSegment(file, committedPosition, 0)
    }
}

```



## 索引文件

索引文件的数据格式很简单，它可以看作是Long的数组，索引是对应的分区在数据文件中的起始地址

```shell
-----------------------------------------------------------------------------------
       Long        |        Long        |        Long         |        Long       |
-----------------------------------------------------------------------------------
   分区一的偏移量   |    分区二的偏移量    |     分区三的偏移量   |     分区四的偏移量
------------------------------------------------------------------------------------
```

IndexShuffleBlockResolver类负责创建索引文件，存储到ShuffleIndexBlock数据块中。它提供了writeIndexFileAndCommit方法创建索引。因为创建索引文件，有线程竞争。所以它会先建立临时索引文件，然后再去检查索引文件是否已经存在，并且与临时索引文件是否相同。如果一致，则删除临时索引文件。如果不一致，则会更新索引文件。writeIndexFileAndCommit方法的代码如下：

```scala
def writeIndexFileAndCommit(
    shuffleId: Int,
    mapId: Int,
    lengths: Array[Long],      // 每个分区对应的数据长度
    dataTmp: File): Unit = {
  // 获取索引文件
  val indexFile = getIndexFile(shuffleId, mapId)
  // 新建临时索引文件
  val indexTmp = Utils.tempFileWith(indexFile)
  try {
    val out = new DataOutputStream(new BufferedOutputStream(new FileOutputStream(indexTmp)))
    Utils.tryWithSafeFinally {
      // 第一个分区的偏移量肯定是从0开始的
      var offset = 0L
      out.writeLong(offset)
      // 根据对应分区的数据长度，计算出偏移量
      for (length <- lengths) {
        offset += length
        out.writeLong(offset)
      }
    } {
      out.close()
    }
    // 获取数据文件
    val dataFile = getDataFile(shuffleId, mapId)
    // 使用synchronized，保证下列程序是原子性的
    synchronized {
      // 调用checkIndexAndDataFile方法，
      // 检查索引文件是否与数据文件匹配，还有是否已经存在和临时索引文件相同的索引文件
      val existingLengths = checkIndexAndDataFile(indexFile, dataFile, lengths.length)
      if (existingLengths != null) {
        // 这里表示别的线程已经创建了正确的索引文件
        // 所以这儿需要删除临时索引文件和对应的临时数据文件
        System.arraycopy(existingLengths, 0, lengths, 0, lengths.length)
        if (dataTmp != null && dataTmp.exists()) {
          dataTmp.delete()
        }
        indexTmp.delete()
      } else {
        // 这里表示没有创建正确的索引文件，所以需要删除原有索引文件和对应的数据文件
        if (indexFile.exists()) {
          indexFile.delete()
        }
        
        if (dataFile.exists()) {
          dataFile.delete()
        }
        // 将临时索引文件重命名，为索引文件
        if (!indexTmp.renameTo(indexFile)) {
          throw new IOException("fail to rename file " + indexTmp + " to " + indexFile)
        }
        // 将临时数据文件重命名，为数据文件
        if (dataTmp != null && dataTmp.exists() && !dataTmp.renameTo(dataFile)) {
          throw new IOException("fail to rename file " + dataTmp + " to " + dataFile)
        }
      }
    }
  } finally {
    if (indexTmp.exists() && !indexTmp.delete()) {
      logError(s"Failed to delete temporary index file at ${indexTmp.getAbsolutePath}")
    }
  }
}
```



## BypassMergeSortShuffleHandle 原理

BypassMergeSortShuffleHandle算法适用于没有聚合，数据量不大的场景。它为 reduce端的每个分区，创建一个DiskBlockObjectWriter。根据Key判断分区索引，然后添加到对应的DiskBlockObjectWriter，写入到文件。 最后按照分区索引顺序，将所有的文件汇合到同一个文件。如下图所示：

![spark-shuffle-bypass](spark-shuffle-bypass.svg)

接下来看看源码的实现

```java
final class BypassMergeSortShuffleWriter<K, V> extends ShuffleWriter<K, V> {
  
  public void write(Iterator<Product2<K, V>> records) throws IOException {
    final SerializerInstance serInstance = serializer.newInstance();
    final long openStartTime = System.nanoTime();
    // DiskBlockObjectWriter数组，索引是reduce端的分区索引
    partitionWriters = new DiskBlockObjectWriter[numPartitions];
    // FileSegment数组，索引是reduce端的分区索引
    partitionWriterSegments = new FileSegment[numPartitions];
    // 为每个reduce端的分区，创建临时Block和文件
    for (int i = 0; i < numPartitions; i++) {
      final Tuple2<TempShuffleBlockId, File> tempShuffleBlockIdPlusFile =
        blockManager.diskBlockManager().createTempShuffleBlock();
      final File file = tempShuffleBlockIdPlusFile._2();
      final BlockId blockId = tempShuffleBlockIdPlusFile._1();
      partitionWriters[i] =
        blockManager.getDiskWriter(blockId, file, serInstance, fileBufferSize, writeMetrics);
    }
   
    // 遍历数据，根据key找到分区索引，存到对应的文件中
    while (records.hasNext()) {
      final Product2<K, V> record = records.next();
      // 获取数据的key
      final K key = record._1();
      // 根据reduce端的分区器，判断该条数据应该存在reduce端的哪个分区
      // 并且通过DiskBlockObjectWriter，存到对应的文件中
      partitionWriters[partitioner.getPartition(key)].write(key, record._2());
    }

    // 调用DiskBlockObjectWriter的commitAndGet方法，获取FileSegment，包含写入的数据信息
    for (int i = 0; i < numPartitions; i++) {
      final DiskBlockObjectWriter writer = partitionWriters[i];
      partitionWriterSegments[i] = writer.commitAndGet();
      writer.close();
    }
    // 获取最终结果的文件名
    File output = shuffleBlockResolver.getDataFile(shuffleId, mapId);
    // 根据output文件名，生成临时文件。临时文件的名称只是在output文件名后面添加了一个uuid
    File tmp = Utils.tempFileWith(output);
    try {
      // 将所有的文件都合并到tmp文件中，返回每个数据段的长度
      partitionLengths = writePartitionedFile(tmp);
      // 这里writeIndexFileAndCommit会将tmp文件重命名，并且会创建索引文件。
      shuffleBlockResolver.writeIndexFileAndCommit(shuffleId, mapId, partitionLengths, tmp);
    } finally {
      if (tmp.exists() && !tmp.delete()) {
        logger.error("Error while deleting temp file {}", tmp.getAbsolutePath());
      }
    }
    mapStatus = MapStatus$.MODULE$.apply(blockManager.shuffleServerId(), partitionLengths);
  }
}
```

从上面的代码可以看到，BypassMergeSortShuffleHandle所有的中间数据都是在磁盘里，并没有利用内存。而且它只保证分区索引的排序，而并不保证数据的排序。



## UnsafeShuffleWriter 原理

UnsafeShuffleWriter会首先将数据序列化，保存在MemoryBlock中。然后将该数据的地址和对应的分区索引，保存在ShuffleInMemorySorter内存中，利用ShuffleInMemorySorter根据分区排序。当内存不足时，会触发spill操作，生成spill文件。最后会将所有的spill文件合并在同一个文件里。

整个过程可以想象成归并排序。ShuffleExternalSorter负责分片的读取数据到内存，然后利用ShuffleInMemorySorter进行排序。排序之后会将结果存储到磁盘文件中。这样就会有很多个已排序的文件， UnsafeShuffleWriter会将所有的文件合并。

 如下图所示，表示了map端一个分区的shuffle过程：

![spark-shuffle-unsafe](spark-shuffle-unsafe.svg)

首先介绍下数据如何存储到MemoryBlock和ShuffleInMemorySorter里。

UnsafeShuffleWriter的insertRecordIntoSorter方法，支持写入单条数据。它会首先序列化数据，再存储到ShuffleExternalSorter。

```java
public class UnsafeShuffleWriter<K, V> extends ShuffleWriter<K, V> {
    
    // 插入单条数据
    void insertRecordIntoSorter(Product2<K, V> record) throws IOException {
      assert(sorter != null);
      // 获取该条数据的key
      final K key = record._1();
      // 根据key计算出，该数据要被分配的分区索引
      final int partitionId = partitioner.getPartition(key);
      // serBuffer存储序列化之后的数据，每一次序列化数据之前，都会清空
      serBuffer.reset();
      // serOutputStream流是在serBuffer外层包装的，通过它实现序列化的写入
      serOutputStream.writeKey(key, OBJECT_CLASS_TAG);
      serOutputStream.writeValue(record._2(), OBJECT_CLASS_TAG);
      serOutputStream.flush();

      final int serializedRecordSize = serBuffer.size();
      assert (serializedRecordSize > 0);
      // 调用ShuffleExternalSorter的insertRecord方法写入数据
      sorter.insertRecord(
        serBuffer.getBuf(), Platform.BYTE_ARRAY_OFFSET, serializedRecordSize, partitionId);
    }
}
```

ShuffleExternalSorter会将读取数据，将数据存到内存中，并将数据地址添加到ShuffleInMemorySorter，并排序。ShuffleExternalSorter的原理会比较复杂，它会涉及到spill操作。

这里先简单的介绍下数据的写入过程 

```java
final class ShuffleExternalSorter extends MemoryConsumer {
    
    // 当前内存块，当触发spill操作时，会重新申请内存块
    private MemoryBlock currentPage = null;
    // 表示当前内存块的写位置
    private long pageCursor = -1;
  
    // recordBase表示数据对象的起始地址，
    // recordOffset表示数据的偏移量（相对于 recordBase）
    // length表示数据的长度
    public void insertRecord(Object recordBase, long recordOffset, int length, int partitionId)
      throws IOException {
    // 当inMemSorter的数据条数过大时，会触发spill
    if (inMemSorter.numRecords() >= numElementsForSpillThreshold) {
      logger.info("Spilling data because number of spilledRecords crossed the threshold " +
        numElementsForSpillThreshold);
      spill();
    }

    // 申请内存
    growPointerArrayIfNecessary();
    // Need 4 bytes to store the record length.
    final int required = length + 4;
    acquireNewPageIfNecessary(required);

    
    final Object base = currentPage.getBaseObject();
    // 将数据所在MemoryBlock的pageNum 和 起始位置 压缩成一个Long类型
    final long recordAddress = taskMemoryManager.encodePageNumberAndOffset(currentPage, pageCursor);
    // 向 MemoryBlock 写入数据长度，使用4个byte表示
    Platform.putInt(base, pageCursor, length);
    pageCursor += 4;
    // 拷贝数据到MemoryBlock
    Platform.copyMemory(recordBase, recordOffset, base, pageCursor, length);
    pageCursor += length;
    // 调用insertRecord方法添加到inMemSorter
    inMemSorter.insertRecord(recordAddress, partitionId);
  }
}
```



ShuffleInMemorySorter支持数据按照分区索引排序。ShuffleInMemorySorter会将数据地址和分区索引，压缩在一个Long类型。一个Long类型有64 bit，包含了分区索引，所在的内存块的pageNum，所在内存块中的偏移位置三部分。Long类型占据64bit，格式如下：

```shell
--------------------------------------------------------------------------
     24 bit           |    13 bit             |      27 bit
--------------------------------------------------------------------------
   partitionId        |   memoryBlock page    |      memoryBlock offset
--------------------------------------------------------------------------
```

因为一个MemoryBlock的偏移量只能由27 bit表示，所以ShuffleExternalSorter指定了申请内存块的最大容量，不能超过 1<< 27，也就是不能超过27位。

ShuffleInMemorySorter使用LongArray保存数据。LongArray可以看作是一个Long类型的数组，不过它支持堆内和堆外内存。下面简单看看它的源码：

```java
final class ShuffleInMemorySorter {
  // 存储数据的数组
  private LongArray array;

  // 添加数据， recordPointer表示数据地址， partitionId表示分区索引
  public void insertRecord(long recordPointer, int partitionId) {
    if (!hasSpaceForAnotherRecord()) {
      throw new IllegalStateException("There is no space for new record");
    }
    // 添加到数组， pos表示数组的索引
    array.set(pos, PackedRecordPointer.packPointer(recordPointer, partitionId));
    pos++;
  }
  
  // 排序计算，返回迭代器
  public ShuffleSorterIterator getSortedIterator() {
    int offset = 0;
    // 使用RadixSort排序算法
    if (useRadixSort) {
      offset = RadixSort.sort(
        array, pos,
        PackedRecordPointer.PARTITION_ID_START_BYTE_INDEX,
        PackedRecordPointer.PARTITION_ID_END_BYTE_INDEX, false, false);
    } else {
      // 提取LongArray还未使用的内存
      MemoryBlock unused = new MemoryBlock(
        array.getBaseObject(),
        array.getBaseOffset() + pos * 8L,
        (array.size() - pos) * 8L);
      LongArray buffer = new LongArray(unused);
      Sorter<PackedRecordPointer, LongArray> sorter =
        new Sorter<>(new ShuffleSortDataFormat(buffer));
      // 使用timSort排序算法
      sorter.sort(array, 0, pos, SORT_COMPARATOR);
    }
    return new ShuffleSorterIterator(pos, array, offset);
  }
  
```



然后看看ShuffleExternalSorter的spill原理。ShuffleExternalSorter继承MemoryConsumer，当ShuffleInMemorySorter的数量过大，或者Executor节点的内存不足时，都会触发spill操作。  ShuffleExternalSorter在spill的时候会从ShuffleInMemorySorter获取排序后的数据地址，然后根据地址取出数据，存到文件里面。

```java
final class ShuffleExternalSorter extends MemoryConsumer {

  public long spill(long size, MemoryConsumer trigger) throws IOException {
    if (trigger != this || inMemSorter == null || inMemSorter.numRecords() == 0) {
      return 0L;
    }
    // 调用writeSortedFile方法将结果写入磁盘
    writeSortedFile(false);
    // 释放内存
    final long spillSize = freeMemory();
    // 释放inMemSorter的内存
    inMemSorter.reset();
    return spillSize;
  }
    
  private void writeSortedFile(boolean isLastFile) throws IOException {
    // 调用inMemSorter获取排序后的结果
    final ShuffleInMemorySorter.ShuffleSorterIterator sortedRecords =
      inMemSorter.getSortedIterator();

    // 字节数组，作为缓存使用保存提取的数据
    final byte[] writeBuffer = new byte[DISK_WRITE_BUFFER_SIZE];

    // 创建临时TempShuffleBlock和临时文件
    final Tuple2<TempShuffleBlockId, File> spilledFileInfo =
      blockManager.diskBlockManager().createTempShuffleBlock();
    final File file = spilledFileInfo._2();
    final TempShuffleBlockId blockId = spilledFileInfo._1();
    
    // 保存此次spill的结果信息
    final SpillInfo spillInfo = new SpillInfo(numPartitions, file, blockId);
    final SerializerInstance ser = DummySerializerInstance.INSTANCE;

    // 为该数据创建DiskBlockObjectWriter
    final DiskBlockObjectWriter writer =
      blockManager.getDiskWriter(blockId, file, ser, fileBufferSizeBytes, writeMetricsToUse);

    // 当前遍历的分区索引
    int currentPartition = -1;
    // 遍历排序后的结果
    while (sortedRecords.hasNext()) {
      sortedRecords.loadNext();
      // 获取数据的分区索引
      final int partition = sortedRecords.packedRecordPointer.getPartitionId();
      assert (partition >= currentPartition);
      if (partition != currentPartition) {
        // 遇到下一个分区的数据，需要调用commitAndGet方法，返回数据信息
        if (currentPartition != -1) {
          final FileSegment fileSegment = writer.commitAndGet();
          // 将FileSegment记录到spillInfo
          spillInfo.partitionLengths[currentPartition] = fileSegment.length();
        }
        // 更新当前遍历的分区索引
        currentPartition = partition;
      }
      
      final long recordPointer = sortedRecords.packedRecordPointer.getRecordPointer();
      // 获取数据的起始地址
      final Object recordPage = taskMemoryManager.getPage(recordPointer);
      // 获取数据所在内存块的位置
      final long recordOffsetInPage = taskMemoryManager.getOffsetInPage(recordPointer);
      // 读取数据的长度，长度使用int， 4bytes存储
      int dataRemaining = Platform.getInt(recordPage, recordOffsetInPage);
      long recordReadPosition = recordOffsetInPage + 4; 
      // 拷贝数据到字节数组中，然后写入到 DiskBlockObjectWriter
      while (dataRemaining > 0) {
        final int toTransfer = Math.min(DISK_WRITE_BUFFER_SIZE, dataRemaining);
        Platform.copyMemory(
          recordPage, recordReadPosition, writeBuffer, Platform.BYTE_ARRAY_OFFSET, toTransfer);
        writer.write(writeBuffer, 0, toTransfer);
        recordReadPosition += toTransfer;
        dataRemaining -= toTransfer;
      }
      writer.recordWritten();
    }
    // 将剩余的数据写入文件
    final FileSegment committedSegment = writer.commitAndGet();
    writer.close();
    
    // 将此次spill的数据结果的信息，添加到spills列表中
    if (currentPartition != -1) {
      spillInfo.partitionLengths[currentPartition] = committedSegment.length();
      spills.add(spillInfo);
    }
  }    
```



ShuffleExternalSorter会把每次spill的信息保存到SpillInfo，后面的UnsafeShuffleWriter在合并文件时会用到。SpillInfo有两个重要的属性：

```scala
class SpillInfo {
    
  // 每个对应分区的数据长度 
  final long[] partitionLengths;
    
  // 存储的文件
  final File file;
 }
```



UnsafeShuffleWriter的合并spill文件，代码如下：

```java
public class UnsafeShuffleWriter<K, V> extends ShuffleWriter<K, V> {
    
  private long[] mergeSpills(SpillInfo[] spills, File outputFile) throws IOException {
    // 是否需要压缩
    final boolean compressionEnabled = sparkConf.getBoolean("spark.shuffle.compress", true);
    final CompressionCodec compressionCodec = CompressionCodec$.MODULE$.createCodec(sparkConf);
    // 是否需要快速合并
    final boolean fastMergeEnabled =
      sparkConf.getBoolean("spark.shuffle.unsafe.fastMergeEnabled", true);
    // 编码方式是否支持快速合并
    final boolean fastMergeIsSupported = !compressionEnabled ||
      CompressionCodec$.MODULE$.supportsConcatenationOfSerializedStreams(compressionCodec);
    // 是否指定了shuffle中间数据加密
    final boolean encryptionEnabled = blockManager.serializerManager().encryptionEnabled();
    if (spills.length == 0) {
        new FileOutputStream(outputFile).close(); // Create an empty file
        return new long[partitioner.numPartitions()];
    } else if (spills.length == 1) {
        Files.move(spills[0].file, outputFile);
        return spills[0].partitionLengths;
    } else {
        final long[] partitionLengths;
        if (fastMergeEnabled && fastMergeIsSupported) {
          if (transferToEnabled && !encryptionEnabled) {
            // 不需要加密，则调用mergeSpillsWithTransferTo方法合并
            logger.debug("Using transferTo-based fast merge");
            partitionLengths = mergeSpillsWithTransferTo(spills, outputFile);
          } else {
            // 否则调用mergeSpillsWithFileStream方法合并
            logger.debug("Using fileStream-based fast merge");
            partitionLengths = mergeSpillsWithFileStream(spills, outputFile, null);
          }
        } else {
          // 调用mergeSpillsWithFileStream方法合并
          logger.debug("Using slow merge");
          partitionLengths = mergeSpillsWithFileStream(spills, outputFile, compressionCodec);
        }
      return partitionLengths;
  }
}
```

上面有两个方法mergeSpillsWithTransferTo和mergeSpillsWithFileStream都可以合并文件。

mergeSpillsWithTransferTo方法的原理比较简单，只是简单的按照分区遍历每个文件，调用了nio的transferTo机制，拷贝数据。

```java
private long[] mergeSpillsWithTransferTo(SpillInfo[] spills, File outputFile) throws IOException {
  assert (spills.length >= 2);
  final int numPartitions = partitioner.numPartitions();
  // 保存每个分区的数据长度
  final long[] partitionLengths = new long[numPartitions];
  // 每个分区对应的FileChannel
  final FileChannel[] spillInputChannels = new FileChannel[spills.length];
  // 每个分区对应在输出文件的位置， 默认值为0
  final long[] spillInputChannelPositions = new long[spills.length];
  FileChannel mergedFileOutputChannel = null;
  // 通过SpillInfo的file属性，获取文件输入流
  for (int i = 0; i < spills.length; i++) {
      spillInputChannels[i] = new FileInputStream(spills[i].file).getChannel();
  }
  // 获取输出文件的流
  mergedFileOutputChannel = new FileOutputStream(outputFile, true).getChannel();

  // 按照分区索引，依次遍历所有文件
  for (int partition = 0; partition < numPartitions; partition++) {
      // 遍历文件
      for (int i = 0; i < spills.length; i++) {
        // 获取该分区数据的长度
        final long partitionLengthInSpill = spills[i].partitionLengths[partition];
        final FileChannel spillInputChannel = spillInputChannels[i];
        // 调用了FileChannel的transferTo方法，拷贝数据
        Utils.copyFileStreamNIO(
          spillInputChannel,               // 源文件
          mergedFileOutputChannel,         // 目标文件 
          spillInputChannelPositions[i],   // 在输出文件的位置
          partitionLengthInSpill);         // 数据长度
        // 更新该分区的数据对应输出文件的位置
        spillInputChannelPositions[i] += partitionLengthInSpill;
        // 更新该分区的数据的长度
        partitionLengths[partition] += partitionLengthInSpill;
      }
  }

  return partitionLengths;
}
```

mergeSpillsWithFileStream的原理和mergeSpillsWithTransferTo差不多，只不过封装了文件流，增加了加密和压缩的功能。

综上所述，UnsafeShuffleWriter会利用内存存储和排序，当内存不足时，会溢写到磁盘。而且它只保证分区索引的排序，而并不保证数据的排序。

## SortShuffleWriter

SortShuffleWriter它支持聚合和排序， 原理会比较复杂。因为篇幅有限，具体原理可以参考这篇博客 {% post_link  spark-shuffle-sort-writer-2 Spark SortShuffleWriter 原理  %} 。

