---
title: Spark MapOutputTracker 原理
date: 2019-01-18 23:33:55
tags: spark, MapOutputTracker
categories: spark
---

# Spark MapOutputTracker 原理

Spark的shuffle过程分为writer和reader两块。 writer负责生成中间数据，reader负责整合中间数据。而中间数据的元信息，则由MapOutputTracker负责管理。 它负责writer和reader的沟通。

shuffle writer会将中间数据保存到Block里面，然后将数据的位置发送给MapOutputTracker。

shuffle reader通过向 MapOutputTracker获取中间数据的位置之后，才能读取到数据。

以下图为例，

![shuffle](map-output-tracker.svg)

rdd1 经过shuffle 生成 rdd2。rdd1有三个分区，对应着三个ShuffleMapTask。每一个ShuffleMapTask执行的结果，对应着一个MapStatus。以rdd1 的 parition 0 分区为例，可以看到它把该分区的数据，根据分区器分成三部分，对应着rdd2 的分区。

shuffle reader 需要读取rdd1 shuffle的中间数据，才能生成 rdd2。 以rdd2的partition 0 分区为例，它需要 rdd1 计算出的数据，找到所有reduce0 的数据。这些数据的位置，需要从MapOutputTracker获取。

shuffle reader 需要提供 shuffleId， mapId, reduceId才能确定一个中间数据。shuffleId表示此次shuffle的唯一id。mapId表示map端 rdd的分区索引，表示由哪个父分区产生的数据。reduceId表示reduce端的分区索引，表示属于子分区的那部分数据。



## 输出信息 MapStatus

MapStatus类表示一个ShuffleMapTask执行返回的结果。MapStatus包含了这个ShuffleMapTask的数据输出信息。

它有两个方法

- location ， 返回数据存储所在 BlockManager 的 Id
- getSizeForBlock， 返回 shufle中间数据中，指定 reduceId 的那部分数据的大小。注意这个值是有精度误差的

以上图的rdd1 的 parition 0 分区为例，它对应着一个ShuffleMapTask。ShuffleMapTask会返回一个MapStatus，该MapStatus会包含了三块数据，分别是reduce0， reduce1， reduce2.

因为一个MapStatus会包含多个reduce的数据信息，这样会占用太多的内存， 所以MapStatus会将结果进行压缩，主要是对长度的压缩。对于不同的reduce数量，对应着不同的子类，CompressedMapStatus 和 HighlyCompressedMapStatus。当reduceId超过了2000， 就使用HighlyCompressedMapStatus。否则使用CompressedMapStatus 。

首先来看看MapStatus压缩数据长度的原理。 对于Long类型的长度，经过log数学运算, 转换为只占一个字节的Byte类型 。虽然结果压缩了，但是精确度却有一定的损失。算法如下，通过对数的方式压缩成整数类型，最大值为255。最大可以表示35GB的长度。

```scala
private[spark] object MapStatus {
    private[this] val LOG_BASE = 1.1
    
    def compressSize(size: Long): Byte = {
    	if (size == 0) {
      		0
    	} else if (size <= 1L) {
      		1
    	} else {
      		math.min(255, math.ceil(math.log(size) / math.log(LOG_BASE)).toInt).toByte
    	}
  }
```

### CompressedMapStatus 压缩原理

CompressedMapStatus的原理比较简单，只是将长度压缩保存。

```scala
private[spark] class CompressedMapStatus(
    private[this] var loc: BlockManagerId,
    private[this] var compressedSizes: Array[Byte])
  extends MapStatus with Externalizable {
      // 接收Long类型的数组，数组的索引是reduceId，索引项是对应的长度
      def this(loc: BlockManagerId, uncompressedSizes: Array[Long]) {
          // 调用上面的compressSize方法， 将Long类型的长度，转换为Byte类型
          this(loc, uncompressedSizes.map(MapStatus.compressSize))
      }
      
      override def getSizeForBlock(reduceId: Int): Long = {
          // 获取压缩的数据长度，然后还原成Long类型
          MapStatus.decompressSize(compressedSizes(reduceId))
      }
```

### HighlyCompressedMapStatus 压缩原理

HighlyCompressedMapStatus适用于reduce数量比较大的情况。对于长度特别大的值，会将长度压缩，单独保存。对于其余的长度，直接返回平均值。

```scala
private[spark] class HighlyCompressedMapStatus private (
    private[this] var loc: BlockManagerId,
    // 哪些reduce数据不为空的数目
    private[this] var numNonEmptyBlocks: Int,
    // reduce数据为空的集合，这里使用了RoaringBitmap，也是为了节省内存
    private[this] var emptyBlocks: RoaringBitmap,
    // 平均长度
    private[this] var avgSize: Long,
    // 长度特别大的reduce集合
    private var hugeBlockSizes: Map[Int, Byte]) {
    
    override def getSizeForBlock(reduceId: Int): Long = {
    	assert(hugeBlockSizes != null)
        // 如果reduce对应的数据为空，则直接返回0
    	if (emptyBlocks.contains(reduceId)) {
      		0
    	} else {
      		hugeBlockSizes.get(reduceId) match {
                 // 如果该reduceId特别大，则从 hugeBlockSizes集合中，获取长度并解压
        		case Some(size) => MapStatus.decompressSize(size)
                // 否则，返回平局长度
        		case None => avgSize
      		}
    	}
  	}
}
```



## MapOutputTracker 相关类

{% plantuml %}

@startuml

abstract class MapOutputTracker

MapOutputTracker <|-- MapOutputTrackerMaster
MapOutputTracker <|-- MapOutputTrackerWorker
MapOutputTrackerMaster -- MapOutputTrackerMasterEndpoint

@enduml

{% endplantuml %}



MapOutputTrackerMaster是运行在driver节点上的，它管理着shuffle的中间数据信息。

MapOutputTrackerWorker是运行在executor节点上的，它会向driver请求shuffle中间数据的信息。

MapOutputTrackerMasterEndpoint是运行在driver节点上的Rpc服务。



## Driver节点的MapOutputTracker

MapOutputTrackerMaster继承MapStatus， 运行在driver节点上。管理所有shuffle的数据信息。所有的shuffle中间数据，都必须在MapOutputTrackerMaster登记。它有两个主要的属性

- mapStatuses， 类型 ConcurrentHashMap[Int, Array[MapStatus]]， Key为shuffleId， Value为该shuffle的MapStatus列表
- shuffleIdLocks， 类型为ConcurrentHashMap[Int, AnyRef]， Key为shuffleId， Value为普通的Object实例，仅仅作为锁存在。

新增shuffle接口

```scala
def registerShuffle(shuffleId: Int, numMaps: Int) {
  if (mapStatuses.put(shuffleId, new Array[MapStatus](numMaps)).isDefined) {
    throw new IllegalArgumentException("Shuffle ID " + shuffleId + " registered twice")
  }
  shuffleIdLocks.putIfAbsent(shuffleId, new Object())
}
```

新增MapStatus接口

```scala
def registerMapOutputs(shuffleId: Int, statuses: Array[MapStatus], changeEpoch: Boolean = false) {
  mapStatuses.put(shuffleId, statuses.clone())
  if (changeEpoch) {
    incrementEpoch()
  }
}
```



MapOutputTrackerMaster有一个队列，存储着请求MapStatus的消息。MapOutputTrackerMasterEndpoint在收到请求后，会将请求添加到这个队列里。MapOutputTrackerMaster还有着一个线程池，来处理队列的消息。

```scala
class MapOutputTrackerMaster {

  // 请求队列
  private val mapOutputRequests = new LinkedBlockingQueue[GetMapOutputMessage]

  // 处理请求的线程池
  private val threadpool: ThreadPoolExecutor = {
    val numThreads = conf.getInt("spark.shuffle.mapOutput.dispatcher.numThreads", 8)
    val pool = ThreadUtils.newDaemonFixedThreadPool(numThreads, "map-output-dispatcher")
    for (i <- 0 until numThreads) {
      pool.execute(new MessageLoop)
    }
    pool
  }
  
  // MessageLoop线程，处理请求
  private class MessageLoop extends Runnable {
    override def run(): Unit = {
      try {
        while (true) {
          try {
            // 取出请求
            val data = mapOutputRequests.take()
             if (data == PoisonPill) {
              // Put PoisonPill back so that other MessageLoops can see it.
              mapOutputRequests.offer(PoisonPill)
              return
            }
            val context = data.context
            val shuffleId = data.shuffleId

            // 获取该shuffle对应的MapStatus
            val mapOutputStatuses = getSerializedMapOutputStatuses(shuffleId)
            // 返回结果给rpc客户端
            context.reply(mapOutputStatuses)
          } catch {
            case NonFatal(e) => logError(e.getMessage, e)
          }
        }
      } catch {
        case ie: InterruptedException => // exit
      }
    }
  }
}
```



上面调用了getSerializedMapOutputStatuses函数，获取请求结果。它涉及到了缓存和序列化。这里缓存了每次的请求结果。对于缓存，必然涉及到缓存失效的问题。MapOutputTrackerMaster使用了epoch代表着数据的版本，cacheEpoch代表着缓存的版本。如果epoch 大于 cacheEpoch， 则表示缓存失效，需要重新获取。

```scala
private[spark] class MapOutputTrackerMaste {
  // 缓存版本
  private var cacheEpoch = epoch
  
  // 缓存请求结果，注意到结果是Byte类型，是序列化之后的数据
  private val cachedSerializedStatuses = new ConcurrentHashMap[Int, Array[Byte]]().asScala
   
  def getSerializedMapOutputStatuses(shuffleId: Int): Array[Byte] = {
    // 该shuffleId 对应的 MapStatus列表
    var statuses: Array[MapStatus] = null
    // 序列化的结果
    var retBytes: Array[Byte] = null
    var epochGotten: Long = -1

    // 检查是否缓存有效，如果有效则返回true，并且设置retBytes的值
    // 如果失效，则返回false
    def checkCachedStatuses(): Boolean = {
      epochLock.synchronized {
        // 如果缓存失效，则清除缓存，并且保持cacheEpoch与epoch一致
        if (epoch > cacheEpoch) {
          cachedSerializedStatuses.clear()
          clearCachedBroadcast()
          cacheEpoch = epoch
        }
        
        cachedSerializedStatuses.get(shuffleId) match {
          // 如果有对应的shuffle缓存，则返回true，设置retBytes的值
          case Some(bytes) =>
            retBytes = bytes
            true
            
          // 如果没有对应的shuffle缓存，则从mapStatuses取出MapStatues
          case None =>
            logDebug("cached status not found for : " + shuffleId)
            statuses = mapStatuses.getOrElse(shuffleId, Array.empty[MapStatus])
            epochGotten = epoch
            false
        }
      }
    }
      
    // 调用checkCachedStatuses， 查看是否有缓存
    if (checkCachedStatuses()) return retBytes  
    
    // 序列化MapStatues列表
    var shuffleIdLock = shuffleIdLocks.get(shuffleId)
      shuffleIdLock.synchronized {
         if (checkCachedStatuses()) return retBytes
         // 序列化
         val (bytes, bcast) = MapOutputTracker.serializeMapStatuses(statuses, broadcastManager,
             isLocal, minSizeForBroadcast)
         // 更新缓存
         epochLock.synchronized {
           if (epoch == epochGotten) {
              cachedSerializedStatuses(shuffleId) = bytes
              if (null != bcast) cachedSerializedBroadcast(shuffleId) = bcast
           } else {
              logInfo("Epoch changed, not caching!")
              removeBroadcast(bcast)
          }
      }
     // 返回序列化的结果
     bytes
   }
}
```



## Executor节点的MapOutputTracker

MapOutputTrackerWorker继承MapOutputTracker，运行在Executor节点。它同样有mapStatuses属性，但这里是表示executor节点的缓存。 

它提供了getStatuses方法，提供给Executor节点，获取指定shuffle的MapStatus。

getStatuses方法，会优先从本地缓存mapStatuses获取，如果没有，则发送GetMapOutputStatuses请求给driver。

```scala
// 正在请求的shuffleId集合
private val fetching = new HashSet[Int]

private def getStatuses(shuffleId: Int): Array[MapStatus] = {
  // 试图从缓存mapStatuses获取结果
  val statuses = mapStatuses.get(shuffleId).orNull
  // 如果mapStatuses没有shuffleId的数据，则会向dirver请求
  if (statuses == null) {
    var fetchedStatuses: Array[MapStatus] = null
    fetching.synchronized {
      // 如果已经在请求该shuffle的MapStatus
      while (fetching.contains(shuffleId)) {
        try {
          // 等待通知
          fetching.wait()
        } catch {
          case e: InterruptedException =>
        }
      }

      // 判断shuffleId的结果是否已经获取到了
      fetchedStatuses = mapStatuses.get(shuffleId).orNull
      if (fetchedStatuses == null) {
        // 没有获取到，则添加到fetching集合
        fetching += shuffleId
      }
    }

    if (fetchedStatuses == null) {
     
      try {
        // askTracker方法里，会去调用Rpc的客户端，发送GetMapOutputStatuses请求
        val fetchedBytes = askTracker[Array[Byte]](GetMapOutputStatuses(shuffleId))
        // 将字节反序列化，得到MapStatus列表
        fetchedStatuses = MapOutputTracker.deserializeMapStatuses(fetchedBytes)
        // 将结果添加到mapStatuses
        mapStatuses.put(shuffleId, fetchedStatuses)
      } finally {
        fetching.synchronized {
          // 通知此shuffleId请求完成
          fetching -= shuffleId
          fetching.notifyAll()
        }
      }
    }

    if (fetchedStatuses != null) {
      return fetchedStatuses
    } else {
      // 如果请求结果，没有该shuffle的输出数据，则抛出异常
      logError("Missing all output locations for shuffle " + shuffleId)
      throw new MetadataFetchFailedException(
        shuffleId, -1, "Missing all output locations for shuffle " + shuffleId)
    }
  } else {
    return statuses
  }
}
```