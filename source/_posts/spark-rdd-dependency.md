---
title: Spark RDD之间的依赖
date: 2019-01-03 21:41:59
tags: spark, rdd, dependency
categories: spark
---

# Spark RDD之间的依赖

RDD表示分布式的数据集，它是由多个分区组成。RDD封装了底层原理，使得我们编程更加抽象，更加方便。 当我们执行RDD的常见操作时(比如map，filter，groupby等transform类型的操作)，就会创造新的RDD，这样RDD之间就有着血统关系。这种关系可以分为两大类，窄依赖和宽依赖。

## 窄依赖

当父RDD的每个分区只对应于子RDD的一个分区，这种情况是窄依赖。

这里分为三种情形：

### OneToOneDependency

父RDD的分区索引与子RDD的分区索引相同，比如map和filter操作, 如下图所示：

![spark-rdd-dependency-one](spark-rdd-dependency-one.svg)

代码原理如下：

```scala
class OneToOneDependency[T](rdd: RDD[T]) extends NarrowDependency[T](rdd) {
  // 返回父RDD对应的partition
  override def getParents(partitionId: Int): List[Int] = List(partitionId)
}
```

### RangeDependency

父RDD的分区索引与子RDD的分区索引，存在线性关系，比如union操作。如下图所示：

![spark-rdd-dependency-range](spark-rdd-dependency-range.svg)

代码如下：

```scala
// inStart表示父RDD的起始索引，一般是0
// outStart表示父RDD对应子RDD的起始索引
// length表示父RDD的分区数目
class RangeDependency[T](rdd: RDD[T], inStart: Int, outStart: Int, length: Int)
  extends NarrowDependency[T](rdd) {
  // 线性计算出父RDD的分区索引
  override def getParents(partitionId: Int): List[Int] = {
    if (partitionId >= outStart && partitionId < outStart + length) {
      List(partitionId - outStart + inStart)
    } else {
      Nil
    }
  }
  
```

### PruneDependency

子RDD的分区数目小于父RDD的分区数目，但子RDD的分区与只对应于父RDD的一个分区。 比如从一个有序的rdd，提取出指定范围内的记录。如下图所示：

![spark-rdd-dependency-prune](spark-rdd-dependency-prune.svg)

代码如下：

```scala
// rdd表示父RDD
// partitionFilterFunc表示需要保留那些分区
class PruneDependency[T](rdd: RDD[T], partitionFilterFunc: Int => Boolean)
  extends NarrowDependency[T](rdd) {

  // 从父RDD提取需要保留的分区
  val partitions: Array[Partition] = rdd.partitions
    .filter(s => partitionFilterFunc(s.index)).zipWithIndex
    // split表示父RDD的分区索引
    // idx表示子RDD的分区索引
    .map { case(split, idx) => new PartitionPruningRDDPartition(idx, split) : Partition }

  override def getParents(partitionId: Int): List[Int] = {
    List(partitions(partitionId).asInstanceOf[PartitionPruningRDDPartition].parentSplit.index)
  }
}
```



## 宽依赖

当父RDD的分区对应于子RDD的多个分区时，两者之间的依赖关系就是宽依赖。当触发shuffle操作时，两者Rdd的关系就是由宽依赖ShuffleDependency表示，它有以下属性：

- rdd， 指向父RDD
- partitioner， 子RDD的分区器
- keyClassName， key值类型
- valueClassName， value值类型
- aggregator， 聚合算法

如下图所示：

![spark-rdd-dependency-shuffle](spark-rdd-dependency-shuffle.svg)



## RDD的种类

RDD的数据计算，涉及到底层分区数据的计算，而分区的计算又涉及到依赖关系。RDD根据分区计算的原理，分为数据源RDD，宽依赖的RDD 和 窄依赖的RDD 三种。

RDD计算分区数据，最重要的的方法就是compute方法。

### 源数据RDD

spark支持读取不同的数据源，如下例子：

- 支持hdfs文件读取， HadoopRDD
- 支持jdbc读取数据库，JdbcRDD

### MapPartitionsRDD

当rdd调用map或filter操作时，会返回MapPartitionsRDD。MapPartitionsRDD对应的依赖关系是OneToOneDependency。

很明显MapPartitionsRDD的compute方法，调用父RDD的iterator方法获取数据，然后调用函数 f 计算值。

```scala
override def compute(split: Partition, context: TaskContext): Iterator[U] =
  f(context, split.index, firstParent[T].iterator(split, context))
```

### PartitionPruningRDD

PartitionPruningRDD也只是简单的从对应的父分区读取数据

```scala
override def compute(split: Partition, context: TaskContext): Iterator[T] = {
  // split的类型是PartitionPruningRDDPartition， 
  // 它有parentSplit，表示父RDD的分区
  firstParent[T].iterator(
    split.asInstanceOf[PartitionPruningRDDPartition].parentSplit, context)
}
```

### ShuffledRDD

```scala
override def compute(split: Partition, context: TaskContext): Iterator[(K, C)] = {
  val dep = dependencies.head.asInstanceOf[ShuffleDependency[K, V, C]]
  SparkEnv.get.shuffleManager.getReader(dep.shuffleHandle, split.index, split.index + 1, context)
    .read()
    .asInstanceOf[Iterator[(K, C)]]
}
```

当rdd调用groupby或reduce操作时，会返回ShuffledRDD。ShuffledRDD对应的关系是ShuffleDependency。它计算分区的数据时，是调用了ShuffleReader读取上一步rdd产生的数据。

