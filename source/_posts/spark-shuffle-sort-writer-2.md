---
title: Spark SortShuffleWriter 原理
date: 2019-01-29 22:06:39
tags: spark, shuffle, SortShuffleWriter 
categories: spark
---

# Spark SortShuffleWriter 原理

在进行shuffle之前，map端会先将数据进行排序。排序的规则，根据不同的场景，会分为两种。首先会根据Key将元素分成不同的partition。第一种只需要保证元素的partitionId排序，但不会保证同一个partitionId的内部排序。第二种是既保证元素的partitionId排序，也会保证同一个partitionId的内部排序。

因为有些shuffle操作涉及到聚合，对于这种需要聚合的操作，使用PartitionedAppendOnlyMap来排序。对于不需要聚合的，则使用PartitionedPairBuffer排序。

两者的关系图如下：

{% plantuml %}

@startuml

class WritablePartitionedPairCollection
class PartitionedAppendOnlyMap
class PartitionedPairBuffer

WritablePartitionedPairCollection <|-- PartitionedAppendOnlyMap
WritablePartitionedPairCollection <|-- PartitionedPairBuffer

@enduml

{% endplantuml %}

WritablePartitionedPairCollection是两者的基类，它提供了排序功能。destructiveSortedWritablePartitionedIterator接收排序规则。

```scala
private[spark] trait WritablePartitionedPairCollection[K, V] {
    
  // 进行排序，返回结果
  // keyComparator指定了key的排序规则，默认按照hashCode比较大小
  def partitionedDestructiveSortedIterator(keyComparator: Option[Comparator[K]])
    : Iterator[((Int, K), V)]
  
  def destructiveSortedWritablePartitionedIterator(keyComparator: Option[Comparator[K]])
    : WritablePartitionedIterator = {
    // 获取排序后的结果
    val it = partitionedDestructiveSortedIterator(keyComparator)
    // WritablePartitionedIterator提供写入磁盘的方法
    new WritablePartitionedIterator {
      private[this] var cur = if (it.hasNext) it.next() else null

      def writeNext(writer: DiskBlockObjectWriter): Unit = {
        writer.write(cur._1._2, cur._2)
        cur = if (it.hasNext) it.next() else null
      }

      def hasNext(): Boolean = cur != null

      def nextPartition(): Int = cur._1._1
    }
  }
}
```

WritablePartitionedPairCollection还提供了封装排序的方法。partitionKeyComparator方法接收一个基于key排序的Comparator， 在包装封装了一层。排序需要先考虑分区，再考虑key排序

```scala
private[spark] object WritablePartitionedPairCollection {
    
  def partitionKeyComparator[K](keyComparator: Comparator[K]): Comparator[(Int, K)] = {
    new Comparator[(Int, K)] {
     
      override def compare(a: (Int, K), b: (Int, K)): Int = {
        val partitionDiff = a._1 - b._1
        if (partitionDiff != 0) {
          partitionDiff
        } else {
          keyComparator.compare(a._2, b._2)
        }
      }
    }
  }
}
```



## PartitionedPairBuffer 原理

当只使用rdd的groupby的方法，那么这里只需要分组，而不需要聚合。这种情况会使用PartitionedPairBuffer排序。首先调用PartitionedPairBuffer的insert方法，向PartitionedPairBuffer添加元素，最后调用partitionedDestructiveSortedIterator获取排序后的结果。

PartitionedPairBuffer的原理很简单，它使用数组保存数据，最后调用Tim排序算法，根据分区索引排序。

数据格式如下：

```shell
--------------------------------------------------------------------------
     AnyRef            |   AnyRef   |       AnyRef           |   AnyRef  |
--------------------------------------------------------------------------
   partitionId, key    |   value    |     partitionId, key   |    value  | 
--------------------------------------------------------------------------

```

PartitionedPairBuffer使用Array[AnyRef] 数组，AnyRef指向两种格式的数据，一种是(parition, key)的元组， 一种是value。

```scala
class PartitionedPairBuffer {
    
    // 使用AnyRef数组，保存数据
    private var data = new Array[AnyRef](2 * initialCapacity)
    // 数组目前的索引
    private var curSize = 0
    // 数组的容量
    private var capacity = initialCapacity
    
    // 添加数据
    def insert(partition: Int, key: K, value: V): Unit = {
       if (curSize == capacity) {
           // 增大数组的容量
           growArray()
       }
       // // 存储（partition， key）元素
       data(2 * curSize) = (partition, key.asInstanceOf[AnyRef])
       // 写入 value
       data(2 * curSize + 1) = value.asInstanceOf[AnyRef]
       curSize += 1
    }
}
```

partitionedDestructiveSortedIterator使用了TimSort算法。TimSort的 定义在org.apache.spark.util.collection包里，具体原理可以搜索。当Sorter排序后，结果仍在存在data数组里。这里封装了data的访问方式，提供了Iterator的遍历。

```scala
override def partitionedDestructiveSortedIterator(keyComparator: Option[Comparator[K]])
    : Iterator[((Int, K), V)] = {
    // 调用partitionKeyComparator封装keyComparator
    // 如果没有指定keyComparator，则返回只根据分区号排序的Comparator
    val comparator = keyComparator.map(partitionKeyComparator).getOrElse(partitionComparator)
    new Sorter(new KVArraySortDataFormat[(Int, K), AnyRef]).sort(data, 0, curSize, comparator)
    iterator
  }
```



## PartitionedAppendOnlyMap 原理

如果shuffle涉及到聚合， 这种情况会使用PartitionedAppendOnlyMap排序。PartitionedAppendOnlyMap提供changeValue方法，添加和合并数据，完成聚合操作。聚合操作的原理可以参见这篇博客  {% post_link  spark-shuffle-aggregator Spark 聚合原理 %} 。

PartitionedAppendOnlyMap自己实现了哈希表，采用了二次探测算法避免哈希冲突。

```scala
class AppendOnlyMap[K, V](initialCapacity: Int = 64) {
  
  // 因为一条数据，占用array的两个位置。所以数组的大小为 数据的容量的两倍
  private var data = new Array[AnyRef](2 * capacity)  
  private var capacity = nextPowerOf2(initialCapacity)
  
  // hash算法，计算出初始地址
  private def rehash(h: Int): Int = Hashing.murmur3_32().hashInt(h).asInt()
    
  // 提供修改数据，这里涉及到聚合操作
  // updateFunc函数接收两个参数，第一个参数表示这个位置是否已经有数据
  // 第二个参数表示原有的数据
  def changeValue(key: K, updateFunc: (Boolean, V) => V): V = {
    val k = key.asInstanceOf[AnyRef]
    if (k.eq(null)) {
      if (!haveNullValue) {
        incrementSize()
      }
      nullValue = updateFunc(haveNullValue, nullValue)
      haveNullValue = true
      return nullValue
    }
    // 使用二次探测算法，找到数据的位置
    var pos = rehash(k.hashCode) & mask
    var i = 1
    while (true) {
      val curKey = data(2 * pos)
      if (curKey.eq(null)) {
        // 如果当前位置没数据，则调用updateFunc函数，返回新的值
        val newValue = updateFunc(false, null.asInstanceOf[V])
        data(2 * pos) = k
        data(2 * pos + 1) = newValue.asInstanceOf[AnyRef]
        incrementSize()
        return newValue
      } else if (k.eq(curKey) || k.equals(curKey)) {
        // 如果当前位置有数据，则调用updateFunc函数，返回新的值
        val newValue = updateFunc(true, data(2 * pos + 1).asInstanceOf[V])
        data(2 * pos + 1) = newValue.asInstanceOf[AnyRef]
        return newValue
      } else {
        // 更新pos位置直到找到key
        val delta = i
        pos = (pos + delta) & mask
        i += 1
      }
    }
    null.asInstanceOf[V] // Never reached but needed to keep compiler happy
  }
}
```



PartitionedAppendOnlyMap自己使用了数组来实现哈希表，因为它使用了二次探测算法避免哈希冲突，所以有可能数组并没有存储满。

```scala
def partitionedDestructiveSortedIterator(keyComparator: Option[Comparator[K]])
  : Iterator[((Int, K), V)] = {
  // 如果没有指定keyComparator，则返回只根据分区号排序的Comparator
  val comparator = keyComparator.map(partitionKeyComparator).getOrElse(partitionComparator)
  // 调用destructiveSortedIterator方法排序
  destructiveSortedIterator(comparator)
}


def destructiveSortedIterator(keyComparator: Comparator[K]): Iterator[(K, V)] = {
    destroyed = true
    // data是存储着数据的数组，
    // 这里会剔除空的元素，将非空的元素往前移
    // keyIndex代表着遍历元素的位置，newIndex代表着要移动的目标位置
    var keyIndex, newIndex = 0
    while (keyIndex < capacity) {
      if (data(2 * keyIndex) != null) {
        data(2 * newIndex) = data(2 * keyIndex)
        data(2 * newIndex + 1) = data(2 * keyIndex + 1)
        newIndex += 1
      }
      keyIndex += 1
    }
    assert(curSize == newIndex + (if (haveNullValue) 1 else 0))
    // 调用Sorter排序
    new Sorter(new KVArraySortDataFormat[K, AnyRef]).sort(data, 0, newIndex, keyComparator)
    // 将结果以迭代器的形式返回
    new Iterator[(K, V)] {
      var i = 0
      var nullValueReady = haveNullValue
      def hasNext: Boolean = (i < newIndex || nullValueReady)
      def next(): (K, V) = {
        if (nullValueReady) {
          nullValueReady = false
          (null.asInstanceOf[K], nullValue)
        } else {
          val item = (data(2 * i).asInstanceOf[K], data(2 * i + 1).asInstanceOf[V])
          i += 1
          item
        }
      }
    }
  }

```



## 添加数据

SortShuffleWriter使用ExternalSorter进行排序，ExternalSorter会根据是否需要聚合来选择不同的算法。

```scala
private[spark] class ExternalSorter[K, V, C] {

  def insertAll(records: Iterator[Product2[K, V]]): Unit = {
    // TODO: stop combining if we find that the reduction factor isn't high
    val shouldCombine = aggregator.isDefined

    if (shouldCombine) {
      // 涉及到聚合，使用PartitionedAppendOnlyMap算法
      val mergeValue = aggregator.get.mergeValue
      val createCombiner = aggregator.get.createCombiner
      var kv: Product2[K, V] = null
      // 构造聚合函数，如果有旧有值，则调用聚合的mergeValue合并值。否则调用createCombiner实例combiner
      val update = (hadValue: Boolean, oldValue: C) => {
        if (hadValue) mergeValue(oldValue, kv._2) else createCombiner(kv._2)
      }
      while (records.hasNext) {
        addElementsRead()
        kv = records.next()
        // 调用getPartition根据key，获得partitionId， 添加到map里
        map.changeValue((getPartition(kv._1), kv._1), update)
        // 检测是否触发spill
        maybeSpillCollection(usingMap = true)
      }
    } else { 
      // 没有涉及到聚合，使用PartitionedPairBuffer算法
      while (records.hasNext) {
        addElementsRead()
        val kv = records.next()
        buffer.insert(getPartition(kv._1), kv._1, kv._2.asInstanceOf[C])
        // 检测是否触发spill
        maybeSpillCollection(usingMap = false)
      }
    }
  }
}
```



## 溢写文件

上面的maybeSpillCollection方法，当数据过多时，会触发溢写。溢写由spill方法负责，它会将已添加的数据进行排序。

```scala
private def maybeSpillCollection(usingMap: Boolean): Unit = {
    var estimatedSize = 0L
    if (usingMap) {
      // 获取map的大小
      estimatedSize = map.estimateSize()
      // 触发spill过程
      if (maybeSpill(map, estimatedSize)) {
        map = new PartitionedAppendOnlyMap[K, C]
      }
    } else {
      // 获取buffer的大小
      estimatedSize = buffer.estimateSize()
      // 触发spill过程
      if (maybeSpill(buffer, estimatedSize)) {
        buffer = new PartitionedPairBuffer[K, C]
      }
    }
  }

protected def maybeSpill(collection: C, currentMemory: Long): Boolean = {
    var shouldSpill = false
    // 每隔32条数据，检查是否当前使用内存超过限制
    if (elementsRead % 32 == 0 && currentMemory >= myMemoryThreshold) {
      // 申请2倍内存
      val amountToRequest = 2 * currentMemory - myMemoryThreshold
      val granted = acquireMemory(amountToRequest)
      myMemoryThreshold += granted
      // 如果申请之后的内存，仍然不够存储，那么触发spill
      shouldSpill = currentMemory >= myMemoryThreshold
    }
    // 当内存不足，或者数据大小超过了指定值，则执行spill
    shouldSpill = shouldSpill || _elementsRead > numElementsForceSpillThreshold
    if (shouldSpill) {
      _spillCount += 1
      // 调用spill
      spill(collection)
      _elementsRead = 0
      _memoryBytesSpilled += currentMemory
      // 释放数据占用的内存
      releaseMemory()
    }
    // 返回是否触发了spill
    shouldSpill
  }

override protected[this] def spill(collection: WritablePartitionedPairCollection[K, C]): Unit = {
    // 调用destructiveSortedWritablePartitionedIterator获取排序后的结果
    val inMemoryIterator = collection.destructiveSortedWritablePartitionedIterator(comparator)
    // 将排序后的结果写入文件
    val spillFile = spillMemoryIteratorToDisk(inMemoryIterator)
    // 记录文件
    spills += spillFile
  }
```

溢写的结果由SpilledFile表示，它有下列属性描述数据的信息：

- file， 溢写的文件
- elementsPerPartition : Array[Long] ， 代表每个分区对应的数据条数



## 排序原理

上面通过WritablePartitionedPairCollection获取排序结果，它的destructiveSortedWritablePartitionedIterator方法接收了排序参数comparator。根据是否需要对key排序，comparator的原理不同。

### 指定key排序

如果rdd指定了order，那么排序会保证partitionId排序，并且也会保证同一个partitionId的内部排序。

这里比较的数据格式为（partitionId, key）

```scala
def partitionKeyComparator[K](keyComparator: Comparator[K]): Comparator[(Int, K)] = {
  new Comparator[(Int, K)] {
    override def compare(a: (Int, K), b: (Int, K)): Int = {
      // 优先比较partition
      val partitionDiff = a._1 - b._1
      if (partitionDiff != 0) {
        partitionDiff
      } else {
        // 如果partition相等，则继续比较key
        keyComparator.compare(a._2, b._2)
      }
    }
  }
}
```

keyComparator指定了Key的排序规则，在ExternalSorter里定义。这里是比较了两个的hashCode。

```scala
  private val keyComparator: Comparator[K] = ordering.getOrElse(new Comparator[K] {
    override def compare(a: K, b: K): Int = {
      val h1 = if (a == null) 0 else a.hashCode()
      val h2 = if (b == null) 0 else b.hashCode()
      if (h1 < h2) -1 else if (h1 == h2) 0 else 1
    }
  })
```

### 不指定key排序

如果rdd没有指定order，则只需要比较partitionId排序，而不需要对同一个partitionId内部排序。

这里比较的数据格式为（partitionId, key）

```scala
def partitionComparator[K]: Comparator[(Int, K)] = new Comparator[(Int, K)] {
    override def compare(a: (Int, K), b: (Int, K)): Int = {
      a._1 - b._1
    }
  }
```



## 读取spill文件

spill文件首先根据partition分片，每个partitioin分片又被切分成多个batch分片。每个batch分片，存储了多条数据。这里多了个batch分片，是因为DiskBlockObjectWriter的commitAndGet方法触发的，commitAndGet会关闭和新建序列化流。

```shell
------------------------------------------------------------------------------------
                     parition 0                                  |     partition 1
------------------------------------------------------------------------------------
                batch 0              |       batch 1             |   
-------------------------------------------------------------------------------------
record 0   |  record 2  |  record 3  |
```



spill的文件读取由SpillReader负责，SpillReader有几个属性比较重要：

- partitionId， 当前读取record的所在partition
- indexInPartition， 当前读取的record在partition的索引
- batchId， 当前读取的record的所在batch
- lastPartitionId，下一个record所在的partition
- nextPartitionToRead， 要读取的下一个partition

访问数据，也是按照partition的顺序遍历的。通过readNextPartition方法，返回单个partition的数据。

```scala
def readNextPartition(): Iterator[Product2[K, C]] = new Iterator[Product2[K, C]] {
  // 记录当前Iterator的所在partition
  val myPartition = nextPartitionToRead
  nextPartitionToRead += 1

  override def hasNext: Boolean = {
    if (nextItem == null) {
      // 调用SpillReader的readNextItem方法，返回record
      nextItem = readNextItem()
      if (nextItem == null) {
        return false
      }
    }
    assert(lastPartitionId >= myPartition)
    // 如果下一个要读取的record所在的partition，不在等于当前Iterator的所在partition，
    // 也就是当前partition的数据都已经读完
    lastPartitionId == myPartition
  }

  override def next(): Product2[K, C] = {
    if (!hasNext) {
      throw new NoSuchElementException
    }
    val item = nextItem
    nextItem = null
    item
  }
}
```



## 合并spill文件

ExternalSorter提供了writePartitionedFile方法合并结果，写入到一个文件中。这里分为两种情况，一种是整个过程数据量比较小，没有发生溢写。另一个是数据量比较大，发生了溢写。

```scala
def writePartitionedFile(
    blockId: BlockId,
    outputFile: File): Array[Long] = {

  // 分区数据对于数据的长度
  val lengths = new Array[Long](numPartitions)
  // 写入文件的DiskBlockObjectWriter
  val writer = blockManager.getDiskWriter(blockId, outputFile, serInstance, fileBufferSize,
    context.taskMetrics().shuffleWriteMetrics)

  if (spills.isEmpty) {
    // 没有发生溢写的情况
    val collection = if (aggregator.isDefined) map else buffer
    // 直接容器排序，返回结果
    val it = collection.destructiveSortedWritablePartitionedIterator(comparator)
    // 遍历结果
    while (it.hasNext) {
      // 获取当前分区
      val partitionId = it.nextPartition()
      // 将此分区的数据，都写入到文件中
      while (it.hasNext && it.nextPartition() == partitionId) {
        it.writeNext(writer)
      }
      // 每遍历完一个分区的数据，调用commitAndGet返回FileSegment，返回写入数据的信息
      val segment = writer.commitAndGet()
      lengths(partitionId) = segment.length
    }
  } else {
    // 发生溢写的情况
    // 调用partitionedIterator返回排序后的数据,
    // 返回的id表示分区号，elements表示对应的数据
    for ((id, elements) <- this.partitionedIterator) {
      if (elements.hasNext) {
        for (elem <- elements) {
          writer.write(elem._1, elem._2)
        }
        // 每遍历完一个分区的数据，就调用commitAndGet方法
        val segment = writer.commitAndGet()
        lengths(id) = segment.length
      }
    }
  }
  writer.close()
  lengths
}
```



这里调用了partitionedIterator方法排序返回结果，partitionedIterator方法其实是调用了merge方法合并结果。

```scala
// spills表示溢写文件，inMemory表示最后一部分存在内存的数据
private def merge(spills: Seq[SpilledFile], inMemory: Iterator[((Int, K), C)])
    : Iterator[(Int, Iterator[Product2[K, C]])] = {
  // SpillReader负责读spill文件
  val readers = spills.map(new SpillReader(_))
  val inMemBuffered = inMemory.buffered
  // 按照分区号遍历数据
  (0 until numPartitions).iterator.map { p =>
    val inMemIterator = new IteratorForPartition(p, inMemBuffered)
    val iterators = readers.map(_.readNextPartition()) ++ Seq(inMemIterator)
    if (aggregator.isDefined) {
      // 指定了聚合，调用mergeWithAggregation排序
      (p, mergeWithAggregation(
        iterators, aggregator.get.mergeCombiners, keyComparator, ordering.isDefined))
    } else if (ordering.isDefined) {
      // No aggregator given, but we have an ordering (e.g. used by reduce tasks in sortByKey);
     // 指定了order，调用mergeSort合并排序
      (p, mergeSort(iterators, ordering.get))
    } else {
      (p, iterators.iterator.flatten)
    }
  }
}
```



mergeWithAggregation方法的原理是调用了mergeSort方法实现排序。

mergeSort的排序原理采用了并排序的算法，使用PriorityQueue优先队列实现。PriorityQueue存储Iterator，比较大小是将Iterator的第一个数据。每次从最小的Iterator取出数据后，然后将iterator重新插入到PriorityQueue，这样PriorityQueue就会将Iterator重新排序。实例如下图所示，有三部分数据，序列按照从上到下的顺序排列。每一步都会从队列中提取一个最小的元素，并将三部分的数据重新排序

![spark-sort-shuffle-writer](spark-sort-shuffle-writer.svg)

```scala
private def mergeSort(iterators: Seq[Iterator[Product2[K, C]]], comparator: Comparator[K])
    : Iterator[Product2[K, C]] =
{
  val bufferedIters = iterators.filter(_.hasNext).map(_.buffered)
  type Iter = BufferedIterator[Product2[K, C]]
  val heap = new mutable.PriorityQueue[Iter]()(new Ordering[Iter] {
    // Use the reverse of comparator.compare because PriorityQueue dequeues the max
    override 
    def compare(x: Iter, y: Iter): Int = -comparator.compare(x.head._1, y.head._1)
  })
  // 添加所有的iterator
  heap.enqueue(bufferedIters: _*)  // Will contain only the iterators with hasNext = true
  new Iterator[Product2[K, C]] {
    override def hasNext: Boolean = !heap.isEmpty

    override def next(): Product2[K, C] = {
      if (!hasNext) {
        throw new NoSuchElementException
      }
      // 取出最小的iterator
      val firstBuf = heap.dequeue()
      // 从iterator中取第一个数据
      val firstPair = firstBuf.next()
      if (firstBuf.hasNext) {
        // 重新插入到PriorityQueue， 让PriorityQueue重新排序
        heap.enqueue(firstBuf)
      }
      firstPair
    }
  }
}
```

