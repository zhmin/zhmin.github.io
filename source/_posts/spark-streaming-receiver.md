---
title: Spark Streaming 数据源读取
date: 2019-02-13 22:09:47
tags: spark, streaming, receiver
---

# Spark Streaming 数据源读取

  

Spark Streaming 支持多种数据源，数据源的读取涉及到多个组件，流程如下图所示

![spark-streaming-receiver](spark-streaming-receiver.svg)

数据源的读取都是由Receiver负责。Receiver会启动后台线程，持续的拉取数据，发送给ReceiverSupervisorImpl处理。

ReceiverSupervisorImpl是运行在executor端的服务，它会将接收的数转发给BlockGenerator。

BlockGenerator 接收数据后，会先缓存起来。然后每隔一段时间，就会将缓存的数据封装成Block保存起来，然后将Block的信息发送到driver端。

ReceiverTracker运行在 driver 端， 负责提交启动Receiver的任务和接收来自Receiver的请求。

## Receiver 任务运行

Receiver 是运行在executor端的，它是作为spark core的任务运行的。而任务提交是由driver端的ReceiverTracker负责，我们先弄清楚ReceiverTracker是如何提交的

### ReceiverTracker 提交任务

ReceiverTracker首先从DStreamGraph中获取所有的ReceiverInputDStream，然后取得它的Receiver。这样就得到了Receiver列表，然后为每一个Receiver分配一个Executor，运行ReceiverSupervisorImpl服务。ReceiverSupervisorImpl服务会一直占用Executor，直到整个spark streaming停止。

首先来看下是怎么分配 receiver 的运行位置。分配算法由ReceiverSchedulingPolicy类负责，原理如下：

1. 首先根据 receiver 指定的 host 位置，从在该 host 的executor 中，找到对应 receiver 数目最少的那个，分配给 这个 receiver
2. 将剩下没有指定位置的 receiver，从所有 executor 中，找到对应 receiver 数目最少的那个，分配给这个 receiver
3. 如果还有空闲的executor，那么从 receiver 列表中，找到分配 executor 数目最少的那个receiver，然后将这个空闲executor分配给receiver

代码如下，先介绍三个变量：

- hostToExecutors， 每个host对应的executor列表
- scheduledLocations， 每个recevier对应的TaskLocation列表
- numReceiversOnExecutor， 每个executor可能执行receiver的数目

```scala
def scheduleReceivers(
    receivers: Seq[Receiver[_]],
    executors: Seq[ExecutorCacheTaskLocation]): Map[Int, Seq[TaskLocation]] = {
  // 每个host对应的executor列表
  val hostToExecutors = executors.groupBy(_.host)
  // 每个recevier对应的TaskLocation列表
  val scheduledLocations = Array.fill(receivers.length)(new mutable.ArrayBuffer[TaskLocation])
  // 每个executor可能执行receiver的数目, 初始值为0
  val numReceiversOnExecutor = mutable.HashMap[ExecutorCacheTaskLocation, Int]()
  executors.foreach(e => numReceiversOnExecutor(e) = 0)
  
  // 遍历receivers列表
  for (i <- 0 until receivers.length) {
    // 如果该receiver指定了位置，那么提取所在位置的host
    receivers(i).preferredLocation.foreach { host =>
      hostToExecutors.get(host) match {
        case Some(executorsOnHost) =>
          // 从该host中寻找分配receiver数目最少的那个executor
          val leastScheduledExecutor =
            executorsOnHost.minBy(executor => numReceiversOnExecutor(executor))
          // 更新scheduledLocations集合
          scheduledLocations(i) += leastScheduledExecutor
          // 更新numReceiversOnExecutor集合
          numReceiversOnExecutor(leastScheduledExecutor) =
            numReceiversOnExecutor(leastScheduledExecutor) + 1
        case None =>
          // preferredLocation is an unknown host.
          // Note: There are two cases:
          // 1. This executor is not up. But it may be up later.
          // 2. This executor is dead, or it's not a host in the cluster.
          // Currently, simply add host to the scheduled executors.

          // Note: host could be `HDFSCacheTaskLocation`, so use `TaskLocation.apply` to handle
          // this case
          scheduledLocations(i) += TaskLocation(host)
      }
    }
  }

  // 遍历那些没有指定位置的receiver
  for (scheduledLocationsForOneReceiver <- scheduledLocations.filter(_.isEmpty)) {
    // 从executor列表中挑选出，分配receiver数目最小的executor
    val (leastScheduledExecutor, numReceivers) = numReceiversOnExecutor.minBy(_._2)
    // 更新scheduledLocations集合
    scheduledLocationsForOneReceiver += leastScheduledExecutor
    // 更新numReceiversOnExecutor集合
    numReceiversOnExecutor(leastScheduledExecutor) = numReceivers + 1
  }

  // 如果还有空闲的executor
  val idleExecutors = numReceiversOnExecutor.filter(_._2 == 0).map(_._1)
  for (executor <- idleExecutors) {
    // 选择出分配executor数目最少的receiver
    val leastScheduledExecutors = scheduledLocations.minBy(_.size)
    // 将这个空闲executor分配给这个receiver
    leastScheduledExecutors += executor
  }
  // 返回 InputDStream 对应 TaskLocaltion的列表
  receivers.map(_.streamId).zip(scheduledLocations).toMap
}
```

获取到receiver的分配位置后，ReceiverTracker的startReceiver方法，负责启动单个Receiver，代码简化如下

```scala
private def startReceiver(
    receiver: Receiver[_],
    scheduledLocations: Seq[TaskLocation]): Unit = {
  
  // 定义Executor端的运行函数
  val startReceiverFunc: Iterator[Receiver[_]] => Unit =
    (iterator: Iterator[Receiver[_]]) => {
        // 取出receiver，这里的rdd只包含一个receiver
        val receiver = iterator.next()
        assert(iterator.hasNext == false)
        // 运行ReceiverSupervisorImpl服务
        val supervisor = new ReceiverSupervisorImpl(
          receiver, SparkEnv.get, serializableHadoopConf.value, checkpointDirOption)
        supervisor.start()
        // 等待服务停止
        supervisor.awaitTermination()
    }

  // 将 receiver 转换为 RDD
  val receiverRDD: RDD[Receiver[_]] =
    if (scheduledLocations.isEmpty) {
      // 如果没有指定Executor位置，则随机分配
      ssc.sc.makeRDD(Seq(receiver), 1)
    } else {
      // 如果指定Executor位置，则传递给makeRDD函数
      val preferredLocations = scheduledLocations.map(_.toString).distinct
      ssc.sc.makeRDD(Seq(receiver -> preferredLocations))
    }
  // 设置RDD的名称
  receiverRDD.setName(s"Receiver $receiverId")
  // 调用sparkContext的方法，提交任务。这里传递了receiverRDD和执行函数startReceiverFunc
  val future = ssc.sparkContext.submitJob[Receiver[_], Unit, Unit](
    receiverRDD, startReceiverFunc, Seq(0), (_, _) => Unit, ())
  // 执行回调函数
  future.onComplete {
    ......
  }(ThreadUtils.sameThread)
}
```

 

### ReceiverSupervisorImpl 启动

上面介绍了 driver 端向 spark 提交了运行 Receiver 的 任务，接下来看看 receiver 在 executor 端的启动过程。executor 端的启动函数是调用了ReceiverSupervisorImpl的 start 方法， ReceiverSupervisorImpl继承ReceiverSupervisor。

```scala
private[streaming] abstract class ReceiverSupervisor(
    receiver: Receiver[_],
    conf: SparkConf
  ) extends Logging {
  
  // 启动方法
  def start() {
    // 子类会重载onStart方法，负责启动时的初始化
    onStart()
    // 启动receiver
    startReceiver()
  }
  
  
  def startReceiver(): Unit = synchronized {
    try {
      // 子类会重载onReceiverStart方法，负责启动receiver前的初始化
      if (onReceiverStart()) {
        logInfo(s"Starting receiver $streamId")
        // 更新状态
        receiverState = Started
        // 调用receiver的onStart方法
        receiver.onStart()
        logInfo(s"Called receiver $streamId onStart")
      } else {
        // The driver refused us
        stop("Registered unsuccessfully because Driver refused to start receiver " + streamId, None)
      }
    } catch {
      case NonFatal(t) =>
        stop("Error starting receiver " + streamId, Some(t))
    }
  }
}  
```



ReceiverSupervisorImpl实现了ReceiverSupervisor的回调方法

```scala
private[streaming] class ReceiverSupervisorImpl(
    receiver: Receiver[_],
    env: SparkEnv,
    hadoopConf: Configuration,
    checkpointDirOption: Option[String]
  ) extends ReceiverSupervisor(receiver, env.conf) with Logging {
  
  // 连接driver端的ReceiverTracker的Rpc客户端
  private val trackerEndpoint = RpcUtils.makeDriverRef("ReceiverTracker", env.conf, env.rpcEnv)
  // BlockGenerator队列
  private val registeredBlockGenerators = new ConcurrentLinkedQueue[BlockGenerator]()
  
  override protected def onStart() {
    // 启动BlockGenerator
    registeredBlockGenerators.asScala.foreach { _.start() }
  }
  
  override protected def onReceiverStart(): Boolean = {
    // 向driver端请求注册receiver，返回结果为true表示注册成功
    val msg = RegisterReceiver(
      streamId, receiver.getClass.getSimpleName, host, executorId, endpoint)
    trackerEndpoint.askSync[Boolean](msg)
  }
```



### Receiver 启动

Receiver类只是一个抽象类，它只提供了保存数据的接口。子类需要实现 onStart 方法，完成初始化，这个方法不能阻塞。以SocketReceiver为例，它的 onStart 方法是启动了一个后台线程，读取 socket 的数据，然后存储到 receiver 里。

```scala
class SocketReceiver[T: ClassTag](
    host: String,
    port: Int,
    bytesToObjects: InputStream => Iterator[T],
    storageLevel: StorageLevel
  ) extends Receiver[T](storageLevel) with Logging {
  
  def onStart() {
    // 创建socket连接
    socket = new Socket(host, port)
    // 开启后台线程，读取socket的数据
    new Thread("Socket Receiver") {
      setDaemon(true)
      // receive方法负责从socket中读取数据
      override def run() { receive() }
    }.start()
  }
    
  def receive() {
    val iterator = bytesToObjects(socket.getInputStream())
    while(!isStopped && iterator.hasNext) {
      // 读取数据，并且调用store方法存储数据
      store(iterator.next())
    }
  }
}
```

从上面的代码可以看到，Receiver提供了 store 方法保存数据。Receiver的数据经过ReceiverSupervisorImpl最终添加到了BlockGenerator的缓存队列。

```scala
abstract class Receiver[T](val storageLevel: StorageLevel) extends Serializable {
  def store(dataItem: T) {
    // 调用了ReceiverSupervisorImpl的pushSingle方法
    supervisor.pushSingle(dataItem)
  }
}

private[streaming] class ReceiverSupervisorImpl {
    def pushSingle(data: Any) {
       // 调用了BlockGenerator的addData方法
       defaultBlockGenerator.addData(data)
    }
}

private[streaming] class BlockGenerator(
    listener: BlockGeneratorListener,
    receiverId: Int,
    conf: SparkConf,
    clock: Clock = new SystemClock()
  ) extends RateLimiter(conf) with Logging {
  
  // 数据缓存队列
  @volatile private var currentBuffer = new ArrayBuffer[Any]
    
  def addData(data: Any): Unit = {
    if (state == Active) {
      waitToPush()
      synchronized {
        if (state == Active) {
          // 添加到缓存队列里
          currentBuffer += data
        } else {
          throw new SparkException(
            "Cannot add data as BlockGenerator has not been started or has been stopped")
        }
      }
    } else {
      throw new SparkException(
        "Cannot add data as BlockGenerator has not been started or has been stopped")
    }
  }
}
```



## BlockGenerator 原理

### 封装Block

数据发送给BlockGenerator后，BlockGenerator会有一个定时的后台线程，将缓存的数据封装成Block。

RecurringTimer类是定时线程，下面的blockIntervalMs参数表示间隔时间，updateCurrentBuffer参数表示执行函数。

updateCurrentBuffer方法会将数据封装成Block，然后发送给队列。

```scala
private[streaming] class BlockGenerator(
  
  // 获取时间间隔
  private val blockIntervalMs = conf.getTimeAsMs("spark.streaming.blockInterval", "200ms")
  // 实例化一个定时器
  private val blockIntervalTimer =
    new RecurringTimer(clock, blockIntervalMs, updateCurrentBuffer, "BlockGenerator")
  // 数据缓存数组
  private var currentBuffer = new ArrayBuffer[Any]
  
  // Block的队列大小  
  private val blockQueueSize = conf.getInt("spark.streaming.blockQueueSize", 10)
  //存储Block的队列
  private val blocksForPushing = new ArrayBlockingQueue[Block](blockQueueSize)
  
  private def updateCurrentBuffer(time: Long): Unit = {
    try {
      var newBlock: Block = null
      // 使用synchronized防止线程竞争
      synchronized {
        if (currentBuffer.nonEmpty) {
          // 将数据保存到 newBlockBuffer
          val newBlockBuffer = currentBuffer
          // 重置 currentBuffer 为空数组
          currentBuffer = new ArrayBuffer[Any]
          // 生成StreamBlock的Id
          val blockId = StreamBlockId(receiverId, time - blockIntervalMs)
          listener.onGenerateBlock(blockId)
          // 生成Block
          newBlock = new Block(blockId, newBlockBuffer)
        }
      }

      if (newBlock != null) {
        // 添加到队列里
        blocksForPushing.put(newBlock)  // put is blocking when queue is full
      }
    } catch {
      case ie: InterruptedException =>
        logInfo("Block updating timer thread was interrupted")
      case e: Exception =>
        reportError("Error in block updating thread", e)
    }
  }
}
```



### 保存和发送Block

BlockGenerator会有一个后台线程，专门负责将队列的Block保存，和发送出去。

```scala
private[streaming] class BlockGenerator {
    
  private val blockPushingThread = new Thread() { override def run() { keepPushingBlocks() } }
    
  private def keepPushingBlocks() {

    def areBlocksBeingGenerated: Boolean = synchronized {
      state != StoppedGeneratingBlocks
    }
    
    try {
      while (areBlocksBeingGenerated) {
        // 从队列里获取block
        Option(blocksForPushing.poll(10, TimeUnit.MILLISECONDS)) match {
          // 调用pushBlock方法保存和发送block
          case Some(block) => pushBlock(block)
          case None =>
        }
      }
      // 如果BlockGenerator停止了，则处理队列中剩余的block
      logInfo("Pushing out the last " + blocksForPushing.size() + " blocks")
      while (!blocksForPushing.isEmpty) {
        val block = blocksForPushing.take()
        logDebug(s"Pushing block $block")
        pushBlock(block)
        logInfo("Blocks left to push " + blocksForPushing.size())
      }
      logInfo("Stopped block pushing thread")
    } catch {
      case ie: InterruptedException =>
        logInfo("Block pushing thread was interrupted")
      case e: Exception =>
        reportError("Error in block pushing thread", e)
    }
  }
  
  // pushBlock调用了listener的回调函数
  private def pushBlock(block: Block) {
    listener.onPushBlock(block.id, block.buffer)
    logInfo("Pushed block " + block.id)
  }
}
```



上面的pushBlock调用了listener的方法，这里的listener是ReceiverSupervisorImpl中实例化的。

```scala
private[streaming] class ReceiverSupervisorImpl
  // BlockGeneratorListener的实现
  private val defaultBlockGeneratorListener = new BlockGeneratorListener {
    def onPushBlock(blockId: StreamBlockId, arrayBuffer: ArrayBuffer[_]) {
      // 调用pushArrayBuffer发送block
      pushArrayBuffer(arrayBuffer, None, Some(blockId))
    }
  }

  def pushArrayBuffer(
      arrayBuffer: ArrayBuffer[_],
      metadataOption: Option[Any],
      blockIdOption: Option[StreamBlockId]
    ) {
    pushAndReportBlock(ArrayBufferBlock(arrayBuffer), metadataOption, blockIdOption)
  }

  def pushAndReportBlock(
      receivedBlock: ReceivedBlock,
      metadataOption: Option[Any],
      blockIdOption: Option[StreamBlockId]
    ) {
    val blockId = blockIdOption.getOrElse(nextBlockId)
    // 使用receivedBlockHandler保存block数据
    val blockStoreResult = receivedBlockHandler.storeBlock(blockId, receivedBlock)
    
    val numRecords = blockStoreResult.numRecords
    val blockInfo = ReceivedBlockInfo(streamId, numRecords, metadataOption, blockStoreResult)
    // 将保存结果的信息发送给Driver端
    trackerEndpoint.askSync[Boolean](AddBlock(blockInfo))
  }
}
```



保存Block分为两种，一种是直接保存到BlockManager中，另一种是需要预先写wal日志。详情可以参见这篇博客



## ReceiverTracker 接收 Block 信息

ReceiverTracker运行在 driver 节点上，它启动着一个Rpc服务ReceiverTrackerEndpoint，负责处理从executor端发过来的Block信息。

它有两个重要变量：

- streamIdToUnallocatedBlockQueues， 保存所有InputDStream对应的还未分配的数据
- timeToAllocatedBlocks， 保存了已分配的数据

当ReceiverTrackerEndpoint收到AddBlock请求， 会调用ReceivedBlockTracker的addBlock方法添加数据。

当spark streaming提交Job时，需要先从这儿分配到数据。allocateBlocksToBatch方法负责分配数据。

```scala
private[streaming] class ReceivedBlockTracker(
    conf: SparkConf,
    hadoopConf: Configuration,
    streamIds: Seq[Int],         // 所有InputDStream的id
    clock: Clock,
    recoverFromWriteAheadLog: Boolean,
    checkpointDirOption: Option[String])
  extends Logging {
  
  private type ReceivedBlockQueue = mutable.Queue[ReceivedBlockInfo]
  
  // key为InputDStream的id， Value为对应的Block队列
  private val streamIdToUnallocatedBlockQueues = new mutable.HashMap[Int, ReceivedBlockQueue]
  // Key为数据批次的时间，Value为分配的Blocks集合
  private val timeToAllocatedBlocks = new mutable.HashMap[Time, AllocatedBlocks]
  
  // 获取对应InputDStream的未分配的Block队列
  private def getReceivedBlockQueue(streamId: Int): ReceivedBlockQueue = {
    streamIdToUnallocatedBlockQueues.getOrElseUpdate(streamId, new ReceivedBlockQueue)
  }
      
  //添加Block
  def addBlock(receivedBlockInfo: ReceivedBlockInfo): Boolean = {
    // 如果配置了wal，则写入wal日志
    val writeResult = writeToLog(BlockAdditionEvent(receivedBlockInfo))
    if (writeResult) {
      synchronized {
        // 添加Block信息到streamIdToUnallocatedBlockQueues集合
        getReceivedBlockQueue(receivedBlockInfo.streamId) += receivedBlockInfo
      }
    }
    writeResult
  }

  // 分配Block
  def allocateBlocksToBatch(batchTime: Time): Unit = synchronized {
    // 检测batchTime的时间必须大于上一次分配的时间
    if (lastAllocatedBatchTime == null || batchTime > lastAllocatedBatchTime) {
      // 获取所有InputDStream的待分配数据
      val streamIdToBlocks = streamIds.map { streamId =>
          (streamId, getReceivedBlockQueue(streamId).dequeueAll(x => true))
      }.toMap
      //实例化AllocatedBlocks
      val allocatedBlocks = AllocatedBlocks(streamIdToBlocks)
      if (writeToLog(BatchAllocationEvent(batchTime, allocatedBlocks))) {
        // 将分配的数据，保存到timeToAllocatedBlocks集合
        timeToAllocatedBlocks.put(batchTime, allocatedBlocks)
        // 更新最后一次分配时间
        lastAllocatedBatchTime = batchTime
      } else {
        logInfo(s"Possibly processed batch $batchTime needs to be processed again in WAL recovery")
      }
    } else {
      logInfo(s"Possibly processed batch $batchTime needs to be processed again in WAL recovery")
    }
  }  
}
```