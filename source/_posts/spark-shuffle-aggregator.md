---
title: spark shuffle 聚合原理
date: 2019-01-28 22:28:57
tags: spark, shuffle, aggregator
---

# Spark 聚合原理

spark在触发shuffle的时候，在一些场景下会涉及到聚合操作。聚合操作起到了一个优化整体计算效率的作用。

## 聚合算法简介

Aggregator类表示Spark的聚合算法。 聚合只能用于数据源是key，value类型。

它有三个重要的函数，都在算法中起着不同的作用。里使用了泛型，分别代表着不同的数据类型。K表示数据源key的类型，V表示数据源value的类型，C表示中间数据Combiner的类型:

- createCombiner，V => C，将value数据，返回Combiner类型
- mergeValue，(C, V) => C， 将value数据，合并到Combiner类型
- mergeCombiners， (C, C) => C)， 合并Combiner类型的数据

这里演示一个例子， 如下图所示：

![spark-aggregate](spark-aggregate.svg)

 (key_0, value_0), 	(key_0, value_1), 	(key_0, value_2)

(key_1, value_3),	(key_1, value_4)

首先将相同的Key分组。然后对每一个分组，做聚合。以key_0分组为例

第一步，将第一个value数据转化为Combiner类型。遇到(key_0, value_0)，将value_0 生成 combine_0数据。这里会调用了createCombiner函数

第二步，将后面的value，依次合并到C类型的数据。这就是mergeValue的作用。当遇到(key_0, value_1)，讲述value_1添加到combine_0数据里

第三部，然后继续遇到(key_0, value_2),恰好这时候触发spill，会新建一个combiner_1数据, 将数据value_2添加combiner_1里。

第四部，最后将所有的combiner数据汇总，比如 合并combiner_0 和 combiner_1，这里调用了mergeCombiners函数。

 

## 任务运行

Spark生成Aggregator， 是在combineByKeyWithClassTag方法。它根据前后rdd的分区器是否一样，来判断是否需要shuffle。

```scala
def combineByKeyWithClassTag[C](
    createCombiner: V => C,
    mergeValue: (C, V) => C,
    mergeCombiners: (C, C) => C,
    partitioner: Partitioner,
    mapSideCombine: Boolean = true,
    serializer: Serializer = null)(implicit ct: ClassTag[C]): RDD[(K, C)] = self.withScope {
  // 生成aggregator
  val aggregator = new Aggregator[K, V, C](
    self.context.clean(createCombiner),
    self.context.clean(mergeValue),
    self.context.clean(mergeCombiners))
  if (self.partitioner == Some(partitioner)) {
    // 如果新的分区器和原有的一样，则表示key的分布是一样。所以没必要shuffle，直接调用mapPartitions
    self.mapPartitions(iter => {
      val context = TaskContext.get()
      new InterruptibleIterator(context, aggregator.combineValuesByKey(iter, context))
    }, preservesPartitioning = true)
  } else {
    // 否则，生成ShuffledRDD
    new ShuffledRDD[K, V, C](self, partitioner)
      .setSerializer(serializer)
      .setAggregator(aggregator)
      .setMapSideCombine(mapSideCombine)
  }
}
```

上面的代码可以看到，如果分区器是一样的，这里仅仅是调用了mapPartitions方法。传递的函数是aggregator调用combineValuesByKey方法返回的。阅读下面的代码，这里是调用了ExternalAppendOnlyMap类，实现了聚合的执行。

```scala
class Aggregator[K, V, C] (
    createCombiner: V => C,
    mergeValue: (C, V) => C,
    mergeCombiners: (C, C) => C) {
    
  def combineValuesByKey(
      iter: Iterator[_ <: Product2[K, V]],
      context: TaskContext): Iterator[(K, C)] = {
    val combiners = new ExternalAppendOnlyMap[K, V, C](createCombiner, mergeValue, mergeCombiners)
    combiners.insertAll(iter)
    updateMetrics(context, combiners)
    combiners.iterator
  }
}
```



而如果分区器不是一样的，会生成ShuffleRDD。aggregator会保存在ShuffleRDD里面，提供后续的shuffle计算使用。



## RDD常见聚合操作

这里需要说明下，RDD类指定了隐式转换 ，可以转换成PairRDDFunctions类。PairRDDFunctions类支持聚合操作。

### groupby 操作

当rdd触发groupby操作时，就会触发聚合。先看看Rdd的groupby方法

```scala
def groupBy[K](f: T => K)(implicit kt: ClassTag[K]): RDD[(K, Iterable[T])] = withScope {
  // 使用默认的分区器
  groupBy[K](f, defaultPartitioner(this))
}

def groupBy[K](f: T => K, p: Partitioner)(implicit kt: ClassTag[K], ord: Ordering[K] = null)
  : RDD[(K, Iterable[T])] = withScope {
    val cleanF = sc.clean(f)
    // 首先使用函数f，生成key。调用map返回（key， value）类型的RDD
    // 这里RDD隐式转换成PairRDDFunctions， 然后调用groupByKey方法
    this.map(t => (cleanF(t), t)).groupByKey(p)
}
```

接下来看看groupByKey方法，这里生成了Aggregator的函数。它使用Combiner类型是CompactBuffer。

CompactBuffer可以简单的理解成一个array，只不过将第一个和第二个元素单独存储，将后面的元素存到array里，但是它对外提供了和array一样的接口。这样对于存储数量小的集合，减少了数组的分配。

```scala
def groupByKey(partitioner: Partitioner): RDD[(K, Iterable[V])] = self.withScope {
  // 生成CompactBuffer，然后将value添加到CompactBuffer里
  val createCombiner = (v: V) => CompactBuffer(v)
  // 将新的value添加到CompactBuffer
  val mergeValue = (buf: CompactBuffer[V], v: V) => buf += v
  // 将多个CompactBuffer合并
  val mergeCombiners = (c1: CompactBuffer[V], c2: CompactBuffer[V]) => c1 ++= c2
  // 调用combineByKeyWithClassTag 生成RDD
  val bufs = combineByKeyWithClassTag[CompactBuffer[V]](
    createCombiner, mergeValue, mergeCombiners, partitioner, mapSideCombine = false)
  bufs.asInstanceOf[RDD[(K, Iterable[V])]]
}
```



### reduceByKey 操作

```
def reduceByKey(partitioner: Partitioner, func: (V, V) => V): RDD[(K, V)] = self.withScope {
  // 这里可以看到createCombiner函数，只是返回value值
  // mergeValues函数，是传入的func函数
  // mergeCombiners函数，也还是传入的func函数
  combineByKeyWithClassTag[V]((v: V) => v, func, func, partitioner)
}
 
def reduceByKey(func: (V, V) => V, numPartitions: Int): RDD[(K, V)] = self.withScope {
  reduceByKey(new HashPartitioner(numPartitions), func)
}

def reduceByKey(func: (V, V) => V): RDD[(K, V)] = self.withScope {
  reduceByKey(defaultPartitioner(self), func)
}
```



### countByKey 操作

```scala
def countByKey(): Map[K, Long] = self.withScope {
  // 调用mapValues生成(key, 1)类型的RDD，然后定义了相加的函数
  self.mapValues(_ => 1L).reduceByKey(_ + _).collect().toMap
}
```

