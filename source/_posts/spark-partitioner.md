---
title: Spark Partitioner 原理
date: 2019-01-06 22:11:41
tags: spark, partitioner
categories: spark
---

# 分区器

当RDD触发到shuffle的时候，会将数据重新打乱分配。如下图所示，父RDD经过shuffle将数据重新分配，生成子RDD



分配的原理，由分区器会决定数据分配到子RDD的哪个分区。分区器由Partitioner接口表示

```scala
abstract class Partitioner extends Serializable {
  // 分区的数目
  def numPartitions: Int
  // 计算key对应的分区索引
  def getPartition(key: Any): Int
}
```

spark有两种内置的分区器，HashPartitioner和RangePartitioner。

## HashPartitioner

HashPartitioner的原理很简单，只是计算key的hashcode，然后对新分区的数目取余。所以HashPartitioner最重要的属性是新分区的数量。注意HashPartition不能支持key为数组类型。

```scala
def getPartition(key: Any): Int = key match {
  case null => 0
  case _ => Utils.nonNegativeMod(key.hashCode, numPartitions)
}

def nonNegativeMod(x: Int, mod: Int): Int = {
  val rawMod = x % mod
  rawMod + (if (rawMod < 0) mod else 0)
}
```



## RangePartitioner

RangePartitioner的原理会稍微复杂一些，会遍历rdd的所有分区数据，从每个分区都会采样，然后根据样本，生成新分区的边界值，这样就可以根据key把数据分布到对应的新分区。

### 蓄水池采样

采样算法使用的是蓄水池采样。它的使用场景是从不知道大小的集合里，只需要一次遍历，就能够等概率的提取k个元素。算法原理是：

1. 先从集合的前k个元素，存储到蓄水池
2. 依次遍历余下的元素，比如遍历第m个元素，然后生成[0, m)区间的随机数 i。如果 i 小于 k，则替换掉原来的第 i 个元素

```scala
def reservoirSampleAndCount[T: ClassTag](
    input: Iterator[T],
    k: Int,
    seed: Long = Random.nextLong())
  : (Array[T], Long) = {
  // 结果集合
  val reservoir = new Array[T](k)
  var i = 0
  // 取出前 k 个元素
  while (i < k && input.hasNext) {
    val item = input.next()
    reservoir(i) = item
    i += 1
  }

  if (i < k) {
    // 如果分区的数据都已经遍历完，则直接返回
    val trimReservoir = new Array[T](i)
    System.arraycopy(reservoir, 0, trimReservoir, 0, i)
    // 返回采样结果和已遍历的数目
    (trimReservoir, i)
  } else {
    // 如果数据还有剩余，则进行蓄水池采样
    var l = i.toLong
    val rand = new XORShiftRandom(seed)
    while (input.hasNext) {
      val item = input.next()
      l += 1
      // 生成随机数
      val replacementIndex = (rand.nextDouble() * l).toLong
      if (replacementIndex < k) {
      	// 如果随机数小于k， 则替代原来的元素
        reservoir(replacementIndex.toInt) = item
      }
    }
    // 返回采样结果和已遍历的数目
    (reservoir, l)
  }
}
```

RangePartitioner会调用reservoirSampleAndCount对每个分区采样，并且调用collect返回采样结果。

```scala
def sketch[K : ClassTag](
    rdd: RDD[K],
    sampleSizePerPartition: Int): (Long, Array[(Int, Long, Array[K])]) = {
  val shift = rdd.id
  val sketched = rdd.mapPartitionsWithIndex { (idx, iter) =>
    val seed = byteswap32(idx ^ (shift << 16))
    val (sample, n) = SamplingUtils.reservoirSampleAndCount(
      iter, sampleSizePerPartition, seed)
    Iterator((idx, n, sample))
  }.collect()
  val numItems = sketched.map(_._2).sum
  // 返回采样结果的数目和采样结果
  (numItems, sketched)
}
```



### 生成边界值

采样结果出来后，还需要处理。这里首先会将分区的采样结果分成两种情况，一种是采样的数目小于预期，这种情况需要重新采样，获取足够的样本。

```scala
// 设置采样总数
val sampleSize = math.min(20.0 * partitions, 1e6)
// 每个分区的采样数，这里乘以3，是为了防止某些分区过小，导致采样总量不足
val sampleSizePerPartition = math.ceil(3.0 * sampleSize / rdd.partitions.length).toInt
// 抽样， numItems为遍历的数量， sketched为采样结果
val (numItems, sketched) = RangePartitioner.sketch(rdd.map(_._1), sampleSizePerPartition)

// 计算平均系数，表明每遍历一个元素，获得采样的数目
val fraction = math.min(sampleSize / math.max(numItems, 1L), 1.0)
// 存储结果， 存储类型为 (key, 权重)
val candidates = ArrayBuffer.empty[(K, Float)]
// 采样数目小于预期的partition
val imbalancedPartitions = mutable.Set.empty[Int]
// 遍历采样结果，idx表示分区索引，n表示遍历数目，sample为该分区的样本值
sketched.foreach { case (idx, n, sample) =>
    if (fraction * n > sampleSizePerPartition) {
        // 如果预期的采样结果数量，大于实际的采样结果数量，则该分区需要重新采样
        imbalancedPartitions += idx
    } else {
        // 计算权重，遍历数目越多，则表明可信度更高，权重也越高
        val weight = (n.toDouble / sample.length).toFloat
        // 添加到candidates
        for (key <- sample) {
            candidates += ((key, weight))
        }
    }
    // 分区重新采样
    if (imbalancedPartitions.nonEmpty) {
        // 实例PartitionPruningRDD，使用imbalancedPartitions挑选出，哪些分区需要重新采样
        val imbalanced = new PartitionPruningRDD(rdd.map(_._1), imbalancedPartitions.contains)
        // 调用rdd的sample取样
        val reSampled = imbalanced.sample(withReplacement = false, fraction, seed).collect()
        // 计算权重
        val weight = (1.0 / fraction).toFloat
        // 添加到candidates
        candidates ++= reSampled.map(x => (x, weight))
    }
    // 排序candidates，生成rangeBounds
    RangePartitioner.determineBounds(candidates, partitions)
}

```

生成rangeBounds的原理比较简单，首先将candidates按照key排序。然后根据weight的总和，计算每个分区的元素的weight和。

```scala
def determineBounds[K : Ordering : ClassTag](
    candidates: ArrayBuffer[(K, Float)],
    partitions: Int): Array[K] = {
    // 按照key排序
    val ordered = candidates.sortBy(_._1)
    // 计算权重和
    val sumWeights = ordered.map(_._2.toDouble).sum
    // 计算每个分区的weight和
    val step = sumWeights / partitions
    // 遍历元素
    var cumWeight = 0.0
    while ((i < numCandidates) && (j < partitions - 1)) {
        val (key, weight) = ordered(i)
        cumWeight += weight
        if (cumWeight >= target) {
            // 更新rangeBounder值
            ....
        }
        .....
    }
}
```



## 默认分区器

当触发shuffle，但没有指定partitioner。spark会自动生成默认的分区器。

首先去寻找父类rdd（注意不是所有祖先的rdd，而仅仅是上一级的rdd）的partitioner，则返回其中的最大partitioner (按照分区数量排序)。

如果父类rdd没有指定partitioner，但是spark.default.parallelism有在配置中指定，则使用该数值，创建HashPartitioner。

否则，就找到父类rdd的最大分区数目，使用该数值，创建HashPartitioner。

```scala
def defaultPartitioner(rdd: RDD[_], others: RDD[_]*): Partitioner = {
  val rdds = (Seq(rdd) ++ others)
  val hasPartitioner = rdds.filter(_.partitioner.exists(_.numPartitions > 0))
  if (hasPartitioner.nonEmpty) {
    hasPartitioner.maxBy(_.partitions.length).partitioner.get
  } else {
    if (rdd.context.conf.contains("spark.default.parallelism")) {
      new HashPartitioner(rdd.context.defaultParallelism)
    } else {
      new HashPartitioner(rdds.map(_.partitions.length).max)
    }
  }
}
```



## 自定义partitioner

HashPartitioner的性能会比较好，但是容易造成划分不均衡，这样会导致shuffle倾斜。RangePartitioner的划分会比较公平，但是性能相对比较差，因为它需要遍历一次所有的数据，才能完成抽样。如果我们对于数据本身，有着比较好的理解，那么可以自定义partitioner，既能防止shuffle倾斜，也不需要像RangePartitioner那样遍历一次数据。

比如有一批数据，是关于所有商品销售记录。我们需要计算出各种商品的销售情况。我们根据以往的经验，商品a卖的比较好，占了整个销售总和的10%， 其余的都差不多。那么可以实现自定义partitioner，将商品a的划分到一个分区，其余的商品根据hashCode随机分配。

```scala
class MyPartitioner extends Partitioner {
    // 分区的数目
    def numPartitions: Int = 10
    
    // 计算key对应的分区索引
    def getPartition(key: Any): Int = {
        val value = key.asInstanceOf(String)
        value match {
            case "商品a" => 0
            case _ => Utils.nonNegativeMod(key.hashCode, numPartitions-1) + 1   
        }
    }
}
```