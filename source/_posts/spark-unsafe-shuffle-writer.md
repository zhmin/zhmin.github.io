---
title: Spark UnsafeShuffleWriter 原理
date: 2019-01-27 20:10:52
tags: spark, shuffle writer
categories: spark
---

## 前言

当分区的数目超过了一定大小，不满足 BypassMergeSortShuffleWriter 时候，Spark 会采用 UnsafeShuffleWriter 算法。同样 UnsafeShuffleWriter 也只适用于不需要聚合操作，也不需要排序的场景。

1. 首先将数据序列化，保存在`MemoryBlock`中
2. 计算数据的分区位置
3. 记录数据的分区位置和其所在`MemoryBlock`中的存储位置
4. 然后根据分区位置和存储位置，进行排序

 如下图所示，表示了map端一个分区的shuffle过程：

![spark-shuffle-unsafe](spark-shuffle-unsafe.svg)



首先将数据序列化，保存在MemoryBlock中。然后将该数据的地址和对应的分区索引，保存在ShuffleInMemorySorter内存中，利用ShuffleInMemorySorter根据分区排序。当内存不足时，会触发spill操作，生成spill文件。最后会将所有的spill文件合并在同一个文件里。

整个过程可以想象成归并排序。ShuffleExternalSorter负责分片的读取数据到内存，然后利用ShuffleInMemorySorter进行排序。排序之后会将结果存储到磁盘文件中。这样就会有很多个已排序的文件， UnsafeShuffleWriter会将所有的文件合并。



## 序列化数据

数据序列化由`UnsafeShuffleWriter`类负责，通过查看它的`insertRecordIntoSorter`方法，就能很清楚的明白。

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



## 存储缓存中

`ShuffleExternalSorter`会负责将数据存储到`MemoryBlock`中，然后提出取它的所在位置和分区位置，将其编码存储到`ShuffleInMemorySorter`中。

```scala
final class ShuffleExternalSorter extends MemoryConsumer {
    
    // 已分配的 MemoryBlock链表，用于存储数据
    private final LinkedList<MemoryBlock> allocatedPages = new LinkedList<>();
    
    // 存储数据的位置和分区，后面会使用它进行排序
    private ShuffleInMemorySorter inMemSorter;
}
```

我们通过查看`insertRecord`方法，就能明白两者之前的关系。下面的代码经过简化处理

```java
// recordBase表示数据对象的起始地址，
// recordOffset表示数据的偏移量（相对于 recordBase）
// length表示数据的长度
public void insertRecord(Object recordBase, long recordOffset, int length, int partitionId)
    throws IOException {
    // 这里会额外使用4个字节，用来存储该条数据的长度
    final int required = length + 4;

    // 获取当前MemoryBlock的位置
    final Object base = currentPage.getBaseObject();
    // 将数据所在MemoryBlock的标识和起始位置，压缩成一个Long类型
    final long recordAddress = taskMemoryManager.encodePageNumberAndOffset(currentPage, pageCursor);
    // 向 MemoryBlock 写入数据长度
    Platform.putInt(base, pageCursor, length);
    pageCursor += 4;
    // 拷贝数据到MemoryBlock
    Platform.copyMemory(recordBase, recordOffset, base, pageCursor, length);
    pageCursor += length;
    // 将数据所在的位置，和分区保存到 inMemSorter 中
    inMemSorter.insertRecord(recordAddress, partitionId);
}
```



## 数据分区排序

### 数据格式

`ShuffleInMemorySorter`存储的数据格式如下，将数据地址和分区索引，压缩在一个 64 位的 Long类型。

```shell
--------------------------------------------------------------------------
     24 bit           |    13 bit             |      27 bit
--------------------------------------------------------------------------
   partitionId        |   memoryBlock page    |      memoryBlock offset
--------------------------------------------------------------------------
```

### 排序算法

`ShuffleInMemorySorter`的数据可以看作是一个 Long 数组，会根据 partitionId 来将数据排序。它支持两种排序算法：

1. TimSort 排序算法，它是一种起源于归并排序和插入排序的混合排序算法，具体原理比较复杂，读者可以自行搜索
2. RadixSort 排序算法，它是是一种非比较型整数排序算法，其原理是将整数按位数切割成不同的数字，然后按每个位数分别比较。具体原理比较复杂，读者可以自行搜索

Spark 默认采用`RadixSort `算法，可以通过`spark.shuffle.sort.useRadixSort`配置项指定。不过`RadixSort `算法对内存空间的要求比较高，需要额外的内存空间，大小等于需要排序的数据。而 `TimSort `算法额外只需要一半的空间。

### 内存扩容

`ShuffleInMemorySorter`随着数据的增长，它的容量也会随着扩充。初始内存大小由`spark.shuffle.sort.initialBufferSize`配置项指定，默认为 4KB。每次扩容后的容量是之前的一倍。



## Spill 操作

### 触发条件

上述已经讲完了整个的排序原理，不过都没考虑到内存容量有限的情况，尤其是在大数据的场景中。我们知道`UnsafeShuffleWriter`在执行排序时，会有两个方面使用到内存，一个数据存储到 `MemoryBlock`里，另一种是排序会使用到。所以当这两方面的内存受到限制时，都会出发Spill 操作（将数据持久化到磁盘里，释放内存空间）。

`ShuffleExternalSorter`继承`MemoryConsumer`，数据存储和排序使用的内存都是由`MemoryConsumer`申请的。而`MemoryBlock`的申请是受到内存池的限制，所以当内存池的容量不足时，就会触发 Spill 操作。

还有当需要排序的数据过多时，也会触发 Spill 操作。这个阈值由`spark.shuffle.spill.numElementsForceSpillThreshold`配置项指定，默认是没有限制的。

### Spill 过程

Spill 操作的源码如下：

```java
final class ShuffleExternalSorter extends MemoryConsumer {
    
  public long spill(long size, MemoryConsumer trigger) throws IOException {
    if (trigger != this || inMemSorter == null || inMemSorter.numRecords() == 0) {
      return 0L;
    }
    // 调用writeSortedFile方法将结果写入磁盘
    writeSortedFile(false);
    // 释放数据存储的内存
    final long spillSize = freeMemory();
    // 释放排序内存
    inMemSorter.reset();
    return spillSize;
  }
}  
```

`writeSortedFile`方法的原理比较简单，这里简单说下流程

1. 首先会将数据根据分区排序
2. 然后遍历排序后的结果，依次根据地址取出原始数据，存到文件里面

每次 Spill 操作只会生成一个文件，里面的数据都是排序好的。Spark 使用 `SpillInfo`来表示此次信息

```scala
class SpillInfo {
  // 每个对应分区的数据长度 
  final long[] partitionLengths;
    
  // 存储的文件
  final File file;
 }
```



## 合并文件

Spark 在排序时有可能会触发多次 Spill 操作，所以最后需要将这些 spill 文件合并一起。合并的原理很简单，采用了归并排序算法。很明显对于文件的操作，我们可以尽可能的使用零拷贝技术，Spark 当然也实现了，称之为快速合并。

当数据没有被加密，并且压缩格式支持的话，是可以进行快速合并的。目前支持的压缩格式如下：

1. 没有被压缩
2. snappy 压缩
3. lzf 压缩
4. lz4 压缩
5. zstd 压缩

这里涉及到了下列配置项，

| 配置项                                | 默认值 | 注释               |
| ------------------------------------- | ------ | ------------------ |
| spark.file.transferTo                 | true   | 是否允许使用零拷贝 |
| spark.shuffle.unsafe.fastMergeEnabled | true   | 是否开启快速合并   |
| spark.shuffle.compress                | true   | 是否允许压缩数据   |

