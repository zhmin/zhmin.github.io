---
title: Spark Streaming 运行原理
date: 2019-02-18 21:25:01
tags: spark, streaming, job
categories: spark streaming
---

# Spark Streaming 的运行原理

先来看看spark streaming的一个例子，引用自官网

```scala
import org.apache.spark._
import org.apache.spark.streaming._
import org.apache.spark.streaming.StreamingContext._ // not necessary since Spark 1.3

// Create a local StreamingContext with two working thread and batch interval of 1 second.
// The master requires 2 cores to prevent from a starvation scenario.
val conf = new SparkConf().setMaster("local[2]").setAppName("NetworkWordCount")
val ssc = new StreamingContext(conf, Seconds(1))

// Create a DStream that will connect to hostname:port, like localhost:9999
val lines = ssc.socketTextStream("localhost", 9999)

// Split each line into words
val words = lines.flatMap(_.split(" "))
// Count each word in each batch
val pairs = words.map(word => (word, 1))
val wordCounts = pairs.reduceByKey(_ + _)
// Print the first ten elements of each RDD generated in this DStream to the console
wordCounts.print()
ssc.start() 
ssc.awaitTermination()
```



上面例子的DStream运行的流程如下，

![spark-streaming-dstream](spark-streaming-dstream.svg)

整个Spark Streaming的运行，先由DStream将数据分批，然后生成RDD，最后将任务提交给Spark Core 运行。引用来自官网的图

![streaming-dstream-ops](streaming-dstream-ops.png)



## DStream的原理

DStream 是 spark streaming 的基本单位，表示数据流。它会将数据按照时间间隔，将数据分批，每个批次的数据都会转换为RDD。Dstream的 compute 方法，负责将流转换为RDD，它接收时间参数，表示上次时间到此次时间生成的RDD。不同种类的DStream，生成RDD的原理也不一样，下面依次介绍

### 数据源流

ReceiverInputDStream表示数据源流，它的数据存在spark的BlockManager里。 ReceiverInputDStream流会从ReceiverTracker服务中获取存放数据的Block位置，然后根据位置生成BlockRDD。

```scala
abstract class ReceiverInputDStream[T: ClassTag](_ssc: StreamingContext)
  extends InputDStream[T](_ssc) {
  
  override def compute(validTime: Time): Option[RDD[T]] = {
    val blockRDD = {
      if (validTime < graph.startTime) {
        // If this is called for any time before the start time of the context,
        // then this returns an empty RDD. This may happen when recovering from a
        // driver failure without any write ahead log to recover pre-failure data.
        new BlockRDD[T](ssc.sc, Array.empty)
      } else {
        // 从receiverTracker中获取数据Block的位置信息
        val receiverTracker = ssc.scheduler.receiverTracker
        val blockInfos = receiverTracker.getBlocksOfBatch(validTime).getOrElse(id, Seq.empty)
        // 通知 InputInfoTracker已经获取Block数据
        val inputInfo = StreamInputInfo(id, blockInfos.flatMap(_.numRecords).sum)
        ssc.scheduler.inputInfoTracker.reportInfo(validTime, inputInfo)
        // 根据Block信息，创建BlockRDD
        createBlockRDD(validTime, blockInfos)
      }
    }
    Some(blockRDD)
  }

  private[streaming] def createBlockRDD(time: Time, blockInfos: Seq[ReceivedBlockInfo]): RDD[T] = {
    if (blockInfos.nonEmpty) {
      // 获取 BlockId
      val blockIds = blockInfos.map { _.blockId.asInstanceOf[BlockId] }.toArray
      // 查看是否所有的Block都是wal日志
      val areWALRecordHandlesPresent = blockInfos.forall { _.walRecordHandleOption.nonEmpty }
      if (areWALRecordHandlesPresent) {
        // 如果所有的Block都支持wal，那么返回WALBackedBlockRDD
        val isBlockIdValid = blockInfos.map { _.isBlockIdValid() }.toArray
        val walRecordHandles = blockInfos.map { _.walRecordHandleOption.get }.toArray
        new WriteAheadLogBackedBlockRDD[T](
          ssc.sparkContext, blockIds, walRecordHandles, isBlockIdValid)
      } else {
        // 否则返回BlockRDD
        if (blockInfos.exists(_.walRecordHandleOption.nonEmpty)) {
          if (WriteAheadLogUtils.enableReceiverLog(ssc.conf)) {
            logError("Some blocks do not have Write Ahead Log information; " +
              "this is unexpected and data may not be recoverable after driver failures")
          } else {
            logWarning("Some blocks have Write Ahead Log information; this is unexpected")
          }
        }
        // 保留在blockManager拥有的Block
        val validBlockIds = blockIds.filter { id =>
          ssc.sparkContext.env.blockManager.master.contains(id)
        }
        // 返回BlockRDD
        new BlockRDD[T](ssc.sc, validBlockIds)
      }
    } else {
      // 没有对应的Block数据，返回空的WriteAheadLogBackedBlockRDD或BlockRDD
      if (WriteAheadLogUtils.enableReceiverLog(ssc.conf)) {
        new WriteAheadLogBackedBlockRDD[T](
          ssc.sparkContext, Array.empty, Array.empty, Array.empty)
      } else {
        new BlockRDD[T](ssc.sc, Array.empty)
      }
    }
  }
}
```

这里简单介绍下BlockRDD和WALBackedBlockRDD。BlockRDD会去对应 executor 节点上的BlockManager服务，获取对应的数据。WALBackedBlockRDD支持WAL读取，只有在BlockManager的数据遭到损坏时，才会读取WAL。

### map数据流

当Dsteam调用map后，会返回MappedDStream。

```scala
def map[U: ClassTag](mapFunc: T => U): DStream[U] = ssc.withScope {
  new MappedDStream(this, context.sparkContext.clean(mapFunc))
}
```

MappedDStream的compute方法很简单，通过获取父DStream的RDD，然后调用RDD的map方法。

```scala
class MappedDStream[T: ClassTag, U: ClassTag] (
    parent: DStream[T],
    mapFunc: T => U
  ) extends DStream[U](parent.ssc) {
  
  override def compute(validTime: Time): Option[RDD[U]] = {
    parent.getOrCompute(validTime).map(_.map[U](mapFunc))
  }
}
```



当Dsteam调用flatMap后，会返回FlatMappedDStream。

```scala
def flatMap[U: ClassTag](flatMapFunc: T => TraversableOnce[U]): DStream[U] = ssc.withScope {
  new FlatMappedDStream(this, context.sparkContext.clean(flatMapFunc))
}
```

FlatMappedDStream的compute方法很简单，通过获取父DStream的RDD，然后调用RDD的flatMap方法。

```scala
class FlatMappedDStream[T: ClassTag, U: ClassTag](
    parent: DStream[T],
    flatMapFunc: T => TraversableOnce[U]
  ) extends DStream[U](parent.ssc) {

  override def compute(validTime: Time): Option[RDD[U]] = {
    parent.getOrCompute(validTime).map(_.flatMap(flatMapFunc))
  }
}
```



### reduce 数据流

当Dsteam调用reduceByKey后，会返回ShuffledDStream。Dsteam可以隐式转换PairDStreamFunctions类，reduceByKey的方法定义是在PairDStreamFunctions类。

```scala
def reduceByKey(reduceFunc: (V, V) => V): DStream[(K, V)] = ssc.withScope {
  // 使用默认的分区器
  reduceByKey(reduceFunc, defaultPartitioner())
}

def reduceByKey(
    reduceFunc: (V, V) => V,
    partitioner: Partitioner): DStream[(K, V)] = ssc.withScope {
  // 调用combineByKey方法
  combineByKey((v: V) => v, reduceFunc, reduceFunc, partitioner)
}

def combineByKey[C: ClassTag](
    createCombiner: V => C,
    mergeValue: (C, V) => C,
    mergeCombiner: (C, C) => C,
    partitioner: Partitioner,
    mapSideCombine: Boolean = true): DStream[(K, C)] = ssc.withScope {
  val cleanedCreateCombiner = sparkContext.clean(createCombiner)
  val cleanedMergeValue = sparkContext.clean(mergeValue)
  val cleanedMergeCombiner = sparkContext.clean(mergeCombiner)
  // 返回ShuffledDStream
  new ShuffledDStream[K, V, C](
    self,
    cleanedCreateCombiner,
    cleanedMergeValue,
    cleanedMergeCombiner,
    partitioner,
    mapSideCombine)
}
```

ShuffledDStream的原理也很简单，它也获取父DStream的RDD，然后调用RDD的combineByKey方法生成新的RDD。

```scala
class ShuffledDStream[K: ClassTag, V: ClassTag, C: ClassTag](
    parent: DStream[(K, V)],
    createCombiner: V => C,
    mergeValue: (C, V) => C,
    mergeCombiner: (C, C) => C,
    partitioner: Partitioner,
    mapSideCombine: Boolean = true
  ) extends DStream[(K, C)] (parent.ssc) {

  override def compute(validTime: Time): Option[RDD[(K, C)]] = {
    parent.getOrCompute(validTime) match {
      case Some(rdd) => Some(rdd.combineByKey[C](
          createCombiner, mergeValue, mergeCombiner, partitioner, mapSideCombine))
      case None => None
    }
  }
}
```



### transform 数据流

当Dsteam调用transform后，会返回TransformedDStream。

```scala
def transform[U: ClassTag](transformFunc: RDD[T] => RDD[U]): DStream[U] = ssc.withScope {
  val cleanedF = context.sparkContext.clean(transformFunc, false)
  // 封装transformFunc函数
  transform((r: RDD[T], _: Time) => cleanedF(r))
}

def transform[U: ClassTag](transformFunc: (RDD[T], Time) => RDD[U]): DStream[U] = ssc.withScope {
  val cleanedF = context.sparkContext.clean(transformFunc, false)
  // 包装函数transformFunc，接收rdds列表，对第一个rdd，执行cleanedF操作
  val realTransformFunc = (rdds: Seq[RDD[_]], time: Time) => {
    assert(rdds.length == 1)
    cleanedF(rdds.head.asInstanceOf[RDD[T]], time)
  }
  // 返回TransformedDStream
  new TransformedDStream[U](Seq(this), realTransformFunc)
}
```

继续看TransformedDStream的源码

```scala
class TransformedDStream[U: ClassTag] (
    parents: Seq[DStream[_]],
    transformFunc: (Seq[RDD[_]], Time) => RDD[U]
  ) extends DStream[U](parents.head.ssc) {
  
  override def compute(validTime: Time): Option[RDD[U]] = {
    val parentRDDs = parents.map { parent => parent.getOrCompute(validTime).getOrElse(
      // Guard out against parent DStream that return None instead of Some(rdd) to avoid NPE
      throw new SparkException(s"Couldn't generate RDD from parent at time $validTime"))
    }
    // 调用transformFunc函数，生成新的RDD
    val transformedRDD = transformFunc(parentRDDs, validTime)
    if (transformedRDD == null) {
      throw new SparkException("Transform function must not return null. " +
        "Return SparkContext.emptyRDD() instead to represent no element " +
        "as the result of transformation.")
    }
    Some(transformedRDD)
  }
}
```



### foreach 数据流

当触发action操作时，都会调用DStream的foreach方法，生成ForEachDStream。以print方法为例

```scala
def print(num: Int): Unit = ssc.withScope {
  // 定义foreachFunc函数， 参数为RDD和Time
  def foreachFunc: (RDD[T], Time) => Unit = {
    (rdd: RDD[T], time: Time) => {
      // 这里调用了RDD的take方法，获取数据
      val firstNum = rdd.take(num + 1)
      println("-------------------------------------------")
      println(s"Time: $time")
      println("-------------------------------------------")
      // 打印数据
      firstNum.take(num).foreach(println)
      if (firstNum.length > num) println("...")
      println()
    }
  }
  // 调用foreachRDD方法
  foreachRDD(context.sparkContext.clean(foreachFunc), displayInnerRDDOps = false)
}

def foreachRDD(
    foreachFunc: (RDD[T], Time) => Unit,
    displayInnerRDDOps: Boolean): Unit = {
  // 返回ForEachDStream，并且调用register方法，更新DStreamGraph
  new ForEachDStream(this,
      context.sparkContext.clean(foreachFunc, false), displayInnerRDDOps).register()
}
```

ForEachDStream是用来数据输出的，它的compute方法为空，但是它定义了generateJob方法，生成Job提交给spark运行。

```scala
class ForEachDStream[T: ClassTag] (
    parent: DStream[T],
    foreachFunc: (RDD[T], Time) => Unit,
    displayInnerRDDOps: Boolean
  ) extends DStream[Unit](parent.ssc) {

  override def compute(validTime: Time): Option[RDD[Unit]] = None
  
  override def generateJob(time: Time): Option[Job] = {
    // 定义jobFunc，首先获取上级的RDD，然后传递给foreachFunc函数
    parent.getOrCompute(time) match {
      case Some(rdd) =>
        val jobFunc = () => createRDDWithLocalProperties(time, displayInnerRDDOps) {
          foreachFunc(rdd, time)
        }
        Some(new Job(time, jobFunc))
      case None => None
    }
  }
}
```



## 任务提交

spark streaming会定时的生成rdd，然后生成Job，通过JobScheduler提交给spark context，过程如下图所示

![spark-streaming-job-flow](spark-streaming-job-flow.svg)

首先介绍两个工具类EventLoop类和RecurringTimer，它们在任务提交中都有使用到。

### 时间处理器  EventLoop

EventLoop用于线程之间的通信。它包含了一个任务队列，和一个处理任务的线程。子类需要继承onReceive方法，实现任务的处理。

```scala
private[spark] abstract class EventLoop[E](name: String) extends Logging {
  // 任务队列
  private val eventQueue: BlockingQueue[E] = new LinkedBlockingDeque[E]()
  
  // 处理任务线程
  private val eventThread = new Thread(name) {
    setDaemon(true)
    override def run(): Unit = {
      try {
        while (!stopped.get) {
          // 从队列里取出任务
          val event = eventQueue.take()
          try {
            // 调用onReceive方法处理任务
            onReceive(event)
          } catch {
            case NonFatal(e) =>
              try {
                // 当任务处理出错，会调用onError方法
                onError(e)
              } catch {
                case NonFatal(e) => logError("Unexpected error in " + name, e)
              }
          }
        }
      } catch {
        case ie: InterruptedException => // exit even if eventQueue is not empty
        case NonFatal(e) => logError("Unexpected error in " + name, e)
      }
    }

  }
  
  // 添加任务
  def post(event: E): Unit = {
    eventQueue.put(event)
  }
```



### 定时任务  RecurringTimer

RecurringTimer负责定时回调，它有一个后台线程，定时检查时间。每隔一段时间，就会回调。

```scala
class RecurringTimer(clock: Clock, period: Long, callback: (Long) => Unit, name: String)
  extends Logging {
  // 定时线程
  private val thread = new Thread("RecurringTimer - " + name) {
    setDaemon(true)
    override def run() { loop }
  }
  
  // 线程循环函数
  private def loop() {
    try {
      while (!stopped) {
        // 调用triggerActionForNextInterval定时回调
        triggerActionForNextInterval()
      }
      triggerActionForNextInterval()
    } catch {
      case e: InterruptedException =>
    }
  }
  
  private def triggerActionForNextInterval(): Unit = {
    // nextTime表示下次执行时间
    clock.waitTillTime(nextTime)
    // 调用callback函数
    callback(nextTime)
    // 更新时间
    prevTime = nextTime
    nextTime += period
    logDebug("Callback for " + name + " called at time " + prevTime)
  }  
}
```



### Job生成 JobGenerator

JobGenerator类，它有一个EventLoop实例，处理JobGeneratorEvent事件。

```scala
class JobGenerator(jobScheduler: JobScheduler) extends Logging {
  // 事件处理器  
  private var eventLoop: EventLoop[JobGeneratorEvent] = null
  
  def start(): Unit = synchronized {
    eventLoop = new EventLoop[JobGeneratorEvent]("JobGenerator") {
      // 定义onReceive方法，这里处理任务调用了processEvent方法
      override protected def onReceive(event: JobGeneratorEvent): Unit = processEvent(event)

      override protected def onError(e: Throwable): Unit = {
        jobScheduler.reportError("Error in job generator", e)
      }
    }
    // 启动eventLoop
    eventLoop.start()
  }  
}
```

JobGenerator类还有一个线程，定时发送GenerateJobs事件。

```scala
class JobGenerator(jobScheduler: JobScheduler) extends Logging {
  // 实例化RecurringTimer，定时向eventLoop发送GenerateJobs事件
  // 时间间隔在StreamingContext初始化时指定
  private val timer = new RecurringTimer(clock, ssc.graph.batchDuration.milliseconds,
    longTime => eventLoop.post(GenerateJobs(new Time(longTime))), "JobGenerator")
}
```

JobGenerator当接收到GenerateJobs事件后，就会调用generateJobs方法处理。继续看generateJobs方法，是如何生成Job

```scala
private def generateJobs(time: Time) {
  Try {
    // 通知receiverTracker生成此次Job的数据批次
    jobScheduler.receiverTracker.allocateBlocksToBatch(time) // allocate received blocks to batch
    // 调用DStreamGraph生成Job
    graph.generateJobs(time) // generate jobs using allocated block
  } match {
    case Success(jobs) =>
      // 生成Job成功，然后提交Job
      // 获取该Job的数据输入信息
      val streamIdToInputInfos = jobScheduler.inputInfoTracker.getInfo(time)
      // 通知jobScheduler提交JobSet
      jobScheduler.submitJobSet(JobSet(time, jobs, streamIdToInputInfos))
    case Failure(e) =>
      jobScheduler.reportError("Error generating jobs for time " + time, e)
      PythonDStream.stopStreamingContextIfPythonProcessIsDead(e)
  }
  eventLoop.post(DoCheckpoint(time, clearCheckpointDataLater = false))
}
```

上面使用了DStreamGraph生成Job列表。DStreamGraph包含了所有的输出流，它会为每个输出流都生成一个Job

```scala
final private[streaming] class DStreamGraph extends Serializable with Logging {
  private val outputStreams = new ArrayBuffer[DStream[_]]()

  def generateJobs(time: Time): Seq[Job] = {
    logDebug("Generating jobs for time " + time)
    val jobs = this.synchronized {
      // 遍历输出流
      outputStreams.flatMap { outputStream =>
        // 调用输出流的generateJob方法，生成Job
        val jobOption = outputStream.generateJob(time)
        jobOption.foreach(_.setCallSite(outputStream.creationSite))
        jobOption
      }
    }
    logDebug("Generated " + jobs.length + " jobs for time " + time)
    jobs
  }
}
```



### Job集合

JobSet类包含了Job的集合，和记录了处理信息（开始时间，结束时间）。它提供了对应的接口，来更新JobSet的状态。



### Job调度 JobScheduler

JobScheduler包含了JobSet的集合和一个线程池负责提交Job。

```scala
class JobScheduler(val ssc: StreamingContext) extends Logging {
  // JobSet集合，Key为该JobSet的批次时间
  private val jobSets: java.util.Map[Time, JobSet] = new ConcurrentHashMap[Time, JobSet]
  // 后台线程池，负责提交Job
  private val numConcurrentJobs = ssc.conf.getInt("spark.streaming.concurrentJobs", 1)
  private val jobExecutor =
    ThreadUtils.newDaemonFixedThreadPool(numConcurrentJobs, "streaming-job-executor")
  
  def submitJobSet(jobSet: JobSet) {
    if (jobSet.jobs.isEmpty) {
      logInfo("No jobs added for time " + jobSet.time)
    } else {
      listenerBus.post(StreamingListenerBatchSubmitted(jobSet.toBatchInfo))
      // 添加到jobSets集合
      jobSets.put(jobSet.time, jobSet)
      // 后台线程提交Job
      jobSet.jobs.foreach(job => jobExecutor.execute(new JobHandler(job)))
      logInfo("Added jobs for time " + jobSet.time)
    }
  }
  
  private class JobHandler(job: Job) extends Runnable with Logging {
    import JobScheduler._

    def run() {
      val oldProps = ssc.sparkContext.getLocalProperties
      try {
        var _eventLoop = eventLoop
        if (_eventLoop != null) {
          // 发送JobStarted事件
          _eventLoop.post(JobStarted(job, clock.getTimeMillis()))
          SparkHadoopWriterUtils.disableOutputSpecValidation.withValue(true) {
            // 执行job的run方法，提交Job并且等待完成
            job.run()
          }
          _eventLoop = eventLoop
          if (_eventLoop != null) {
            // 发送JobCompleted事件
            _eventLoop.post(JobCompleted(job, clock.getTimeMillis()))
          }
        } else {
          // JobScheduler has been stopped.
        }
      } finally {
        ssc.sparkContext.setLocalProperties(oldProps)
      }
    }
  }  

```

它还包含了一个事件处理器，负责处理JobSchedulerEvent事件，主要负责更新JobSet的状态。

```scala
class JobScheduler(val ssc: StreamingContext) extends Logging {

  private var eventLoop: EventLoop[JobSchedulerEvent] = null

  // JobScheduler的初始化方法
  def start(): Unit = synchronized {
    // 实例EventLoop， 处理JobSchedulerEvent事件
    eventLoop = new EventLoop[JobSchedulerEvent]("JobScheduler") {
      // 调用processEvent方法处理事件
      override protected def onReceive(event: JobSchedulerEvent): Unit = processEvent(event)

      override protected def onError(e: Throwable): Unit = reportError("Error in job scheduler", e)
    }
    eventLoop.start()
  }

  private def processEvent(event: JobSchedulerEvent) {
    try {
      event match {
        // 处理Job开始
        case JobStarted(job, startTime) => handleJobStart(job, startTime)
        // 处理Job完成
        case JobCompleted(job, completedTime) => handleJobCompletion(job, completedTime)
        // 处理Job失败
        case ErrorReported(m, e) => handleError(m, e)
      }
    } catch {
      case e: Throwable =>
        reportError("Error in job scheduler", e)
    }
  }
    
  private def handleJobStart(job: Job, startTime: Long) {
    // 获取JobSet
    val jobSet = jobSets.get(job.time)
    // 更新jobSet的状态
    jobSet.handleJobStart(job)
    // 设置job的开始执行时间
    job.setStartTime(startTime)
    logInfo("Starting job " + job.id + " from job set of time " + jobSet.time)
  }

  private def handleJobCompletion(job: Job, completedTime: Long) {
    // 获取JobSet
    val jobSet = jobSets.get(job.time)
    // 更新jobSet的状态
    jobSet.handleJobCompletion(job)
    // 设置job的结束时间
    job.setEndTime(completedTime)
    // 处理job执行的结果
    job.result match {
      case Failure(e) =>
        reportError("Error running job " + job, e)
      case _ =>
        // 如果jobSet中所有的job都完成，则从jobSets删除掉
        if (jobSet.hasCompleted) {
          jobSets.remove(jobSet.time)
          // 通知jobGenerator，这个批次的Job已经完成
          jobGenerator.onBatchCompletion(jobSet.time)
          logInfo("Total delay: %.3f s for time %s (execution: %.3f s)".format(
            jobSet.totalDelay / 1000.0, jobSet.time.toString,
            jobSet.processingDelay / 1000.0
          ))
        }
    }
  }
}
```