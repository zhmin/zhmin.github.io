---
title: Spark SortShuffleWriter 原理
date: 2019-01-29 22:06:39
tags: spark, shuffle, SortShuffleWriter 
categories: spark
---

# Spark SortShuffleWriter 原理

## 前言

在数据量大的情况，需要聚合或者排序的情形下，Spark 采用`SortShuffleWriter`完成 map 端的 shuffle 操作。`SortShuffleWriter` 根据是否需要聚合操作，分为两种不同的实现。

1. 对于这种需要聚合的操作，使用`PartitionedAppendOnlyMap`来排序。
2. 对于不需要聚合的，则使用`PartitionedPairBuffer`排序。

## PartitionedPairBuffer 原理

当只使用 rdd 的 groupby 方法，那么这里只需要分组，而不需要聚合。Spark 会采用`PartitionedPairBuffer`来完成分组。`PartitionedPairBuffer`使用数组保存数据，

```scala
class PartitionedPairBuffer {
    // 使用AnyRef数组，保存数据
    private var data = new Array[AnyRef](2 * initialCapacity)

    // 数组的容量
    private var capacity = initialCapacity
}
```

每条数据对应数组的两个位置，格式如下所示：

```shell
-------------------------------------
   partitionId, key    |   value    |
-------------------------------------
```

排序的原理也简单，只是将数组进行排序，比较函数由外部指定。

## PartitionedAppendOnlyMap 原理

如果shuffle涉及到聚合， Spark会先将数据进行分组，完成聚合操作。

我们完成分组一般使用哈希表是最方便，也是最为高效的。哈希表对于碰撞冲突的解决方法，有链表和线性探测法。

1. 链表的优点是实现简单，扩容方便，缺点是内存要求高，像 Java 的 HashMap 的实现就是采用这种。
2. 线性探测法，优点是较位节省内存，效率高，缺点是扩容复杂，原理可以参考https://www.cnblogs.com/longerQiu/p/11703441.html

因为 Spark 也是采用哈希表，来完成分组的。不过 Spark 采用的是线性探测法，不仅仅基于上述的优缺点比较，还因为 Spark 需要分组之后的数据，进行排序。排序会经常移动数据，线性探测法一般使用数组来实现，所以对于排序有着天然的友好性，而基于链表的实现，操作会非常慢。

我们简单的看下`AppendOnlyMap`的实现，它使用了数组来存储值。

```scala
class AppendOnlyMap[K, V](initialCapacity: Int = 64) {
  
  // 因为一条数据的 key 和 value，对应 array 的两个位置。所以数组的大小为 数据的容量的两倍
  private var data = new Array[AnyRef](2 * capacity)
  
  // 数组大小为2的倍数，这样对于计算位置非常方便，直接执行位操作
  private var capacity = nextPowerOf2(initialCapacity)
  
  // 哈希算法采用 murmur3，这种方法计算的值更加平均分布，计算效率也更高
  private def rehash(h: Int): Int = Hashing.murmur3_32().hashInt(h).asInt()
}
```



分组之后，再来讲讲它的排序过程。`AppendOnlyMap`的数组会有空闲位置，所以 Spark 会将这些空闲位置移到数组后面，这样排序的数目会少一些。源码比较简单，这里只是说下过程

1. 将空闲位置移到数组后面，采用了双指针算法
2. 调用TimSort 算法进行排序

我们知道 Spark 在 shuffle 过程中会有 TimSort 和 RadixSort 两种排序算法，这里为什么采用 TimSort。我们可以把 TimSort 看作是归并排序和插入排序的混合排序算法，小片段使用插入排序，总体上是使用归并排序。因为采用了线性探测法实现了哈希表，那么说明相邻位置的数据，可能更加倾向于部分有序，这种数据分布非常适合 TimSort 算法。



## 聚合操作

当指定了聚合操作时，Spark 会使用`PartitionedAppendOnlyMap`来完成。代码如下图所示

```scala
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
}
```



## 写文件

当数据越来越多时，单个`PartitionedAppendOnlyMap `或`PartitionedPairBuffer`的内存也是有限的，所以 Spark 会定期的检查是否需要触发 Spill 操作。当每插入 32 条数据时，就会检查当前的内存使用量。首先 Spark 会有限申请内存，如果申请内存不够，那么就会触发 spill 操作。

```scala
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
```

这里涉及到了两个配置项：

1. `spark.shuffle.spill.numElementsForceSpillThreshold`，数据条数的阈值，默认没有限制
2. `spark.shuffle.spill.initialMemoryThreshold`，初始内存阈值，默认为 5KB

spill 过程也很简单，只是调用了排序，将结果写入到文件中。结果由 `SpilledFile` 表示，它有下列属性描述数据的信息：

- file， 溢写的文件
- elementsPerPartition : Array[Long] ， 代表每个分区对应的数据条数



## 合并文件

ExternalSorter提供了writePartitionedFile方法合并结果，写入到一个文件中。它采用了并排序的算法，使用PriorityQueue优先队列实现。PriorityQueue存储Iterator，比较大小是将Iterator的第一个数据。每次从最小的Iterator取出数据后，然后将iterator重新插入到PriorityQueue，这样PriorityQueue就会将Iterator重新排序。实例如下图所示，有三部分数据，序列按照从上到下的顺序排列。每一步都会从队列中提取一个最小的元素，并将三部分的数据重新排序

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
