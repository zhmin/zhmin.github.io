---
title: Spark Executor 节点运行原理
date: 2019-01-19 23:28:48
tags: spark
categories: spark
---

# Spark Executor 节点运行原理



## Executor运行流程图

![spark-executor-communicate](spark-executor-communicate.svg)



## Executor 节点启动

这里讲的spark运行的场景都是在Yarn上。从这边博客 {% post_link  spark-on-yarn %} ，可以看到Executor节点的启动函数，是CoarseGrainedExecutorBackend的main函数。

```scala
object CoarseGrainedExecutorBackend extends Logging {
  
  def main(args: Array[String]) {
    // 解析参数
    var argv = args.toList
    while (!argv.isEmpty) {
      .....
    }
    
    //   调用 run 函数
    run(driverUrl, executorId, hostname, cores, appId, workerUrl, userClassPath)
    System.exit(0)
  }
  
  def run(
      driverUrl: String,
      executorId: String,
      hostname: String,
      cores: Int,
      appId: String,
      workerUrl: Option[String],
      userClassPath: Seq[URL]) {
      // 以hadoop所使用的用户执行程序 
      SparkHadoopUtil.get.runAsSparkUser { () =>
        Utils.checkHost(hostname)

        // 实例化executor的默认spark配置
        val executorConf = new SparkConf
        // 连接driver的客户端使用的端口号
        val port = executorConf.getInt("spark.executor.port", 0)
        // 实例化客户模式的RpcEnv
        val fetcher = RpcEnv.create(
          "driverPropsFetcher",
          hostname,
          port,
          executorConf,
          new SecurityManager(executorConf),
          clientMode = true)
        // 创建连接driver服务的客户端，
        // 这里的driver是指 CoarseGrainedSchedulerBackend类的 DriverEndpoint服务
        val driver = fetcher.setupEndpointRefByURI(driverUrl)
        // 向driver请求spark配置
        val cfg = driver.askSync[SparkAppConfig](RetrieveSparkAppConfig)
        val props = cfg.sparkProperties ++ Seq[(String, String)](("spark.app.id", appId))
        // 获取完配置后，关闭客户端
        fetcher.shutdown()

        // 根据driver获取的配置，生成executor的配置
        val driverConf = new SparkConf()
        for ((key, value) <- props) {
          if (SparkConf.isExecutorStartupConf(key)) {
            driverConf.setIfMissing(key, value)
          } else {
            driverConf.set(key, value)
          }
        } 

        // 创建Executor的SparkEnv
        val env = SparkEnv.createExecutorEnv(
          driverConf, executorId, hostname, port, cores, cfg.ioEncryptionKey, isLocal = false)

        // 注册并运行CoarseGrainedExecutorBackend Rpc服务
        env.rpcEnv.setupEndpoint("Executor", new CoarseGrainedExecutorBackend(
          env.rpcEnv, driverUrl, executorId, hostname, cores, userClassPath, env))
        // 等待 Rpc服务运行结束
        env.rpcEnv.awaitTermination()
      }
  }
} 
```



## CoarseGrainedExecutorBackend 服务

CoarseGrainedExecutorBackend继承ThreadSafeRpcEndpoint，包装了Executor类，实现对外提供Rpc服务，支持下列接口：

- RegisteredExecutor， 注册executor
- LaunchTask， 执行task
- KillTask， 停止task



```scala
private[spark] class CoarseGrainedExecutorBackend
  extends ThreadSafeRpcEndpoint with ExecutorBackend with Logging {
      
  override def receive: PartialFunction[Any, Unit] = {
    // 接收从driver发送来的消息，实例化Executor
    case RegisteredExecutor =>
      logInfo("Successfully registered with driver")
      try {
        executor = new Executor(executorId, hostname, env, userClassPath, isLocal = false)
      } catch {
        case NonFatal(e) =>
          exitExecutor(1, "Unable to create executor due to " + e.getMessage, e)
      }

    // 接收从driver发来的注册失败的消息
    case RegisterExecutorFailed(message) =>
      // 退出进程
      exitExecutor(1, "Slave registration failed: " + message)

    // 接收从driver发来的task
    case LaunchTask(data) =>
      if (executor == null) {
        exitExecutor(1, "Received LaunchTask command but executor was null")
      } else {
        // 反序列化task
        val taskDesc = TaskDescription.decode(data.value)
        logInfo("Got assigned task " + taskDesc.taskId)
        // 提交task给executor执行
        executor.launchTask(this, taskDesc)
      }
      
    case KillTask(taskId, _, interruptThread, reason) =>
      if (executor == null) {
        exitExecutor(1, "Received KillTask command but executor was null")
      } else {
        // 调用executor杀死任务
        executor.killTask(taskId, interruptThread, reason)
      }

    case StopExecutor =>
      stopping.set(true)
      logInfo("Driver commanded a shutdown")
      // Cannot shutdown here because an ack may need to be sent back to the caller. So send
      // a message to self to actually do the shutdown.
      self.send(Shutdown)

    case Shutdown =>
      stopping.set(true)
      new Thread("CoarseGrainedExecutorBackend-stop-executor") {
        override def run(): Unit = {
          // executor.stop() will call `SparkEnv.stop()` which waits until RpcEnv stops totally.
          // However, if `executor.stop()` runs in some thread of RpcEnv, RpcEnv won't be able to
          // stop until `executor.stop()` returns, which becomes a dead-lock (See SPARK-14180).
          // Therefore, we put this line in a new thread.
          executor.stop()
        }
      }.start()
  }
} 
```



## 执行任务

driver会发送Task给CoarseGrainedExecutorBackend 服务。CoarseGrainedExecutorBackend会转交给Executor类执行。从上面的代码可以看到，是调用了Executor类的launchTask方法。

Executor类有一个线程池threadPool，负责执行任务。该线程池使用newCachedThreadPool类型，没有线程数目限制。它会把接收的任务，丢到这个线程池里面执行。

```scala
class Executor() {
    
    private val threadPool = {
    	val threadFactory = new ThreadFactoryBuilder()
      		.setDaemon(true)
      		.setNameFormat("Executor task launch worker-%d")
      		.setThreadFactory(new ThreadFactory {
        		override def newThread(r: Runnable): Thread =
          			new UninterruptibleThread(r, "unused")
      		})
      	.build()
    	Executors.newCachedThreadPool(threadFactory).asInstanceOf[ThreadPoolExecutor]
    }
    
    // 执行任务
    def launchTask(context: ExecutorBackend, taskDescription: TaskDescription): Unit = {
        // 实例化TaskRunner
        val tr = new TaskRunner(context, taskDescription)
        runningTasks.put(taskDescription.taskId, tr)
        // 将TaskRunner丢给线程池执行
        threadPool.execute(tr)
  	}
}
```

接下来看看TaskRunner的实现，代码简化如下

```scala
class TaskRunner(
    execBackend: ExecutorBackend,
    private val taskDescription: TaskDescription)
  extends Runnable {
      override def run(): Unit = {
          //  
          
          // 通过ExecutorBackend向driver汇报task已开始运行
          execBackend.statusUpdate(taskId, TaskState.RUNNING, EMPTY_BYTE_BUFFER)
          // 反序列化task
          val ser = env.closureSerializer.newInstance()
          task = ser.deserialize[Task[Any]](
          taskDescription.serializedTask, Thread.currentThread.getContextClassLoader)
          
          // 调用Task的run方法
          val value = task.run(
            taskAttemptId = taskId,
            attemptNumber = taskDescription.attemptNumber,
            metricsSystem = env.metricsSystem)
          
          // 结果序列化
          val resultSer = env.serializer.newInstance()
          val valueBytes = resultSer.serialize(value)
          
		 // 将结果通过ExecutorBackend，发送给driver
          execBackend.statusUpdate(taskId, TaskState.FINISHED, serializedResult)
      }
  }
}
          
```



## 心跳服务

Executor节点还需要保持与driver的心跳，否则driver会认为Executor节点异常。Executor类有个线程，专门负责与driver的心跳连接，定时发送给心跳信息。每次心跳都会携带任务的运行信息。

```scala
class Executor() {
    
    // 后台单线程
	private val heartbeater = ThreadUtils.newDaemonSingleThreadScheduledExecutor("driver-heartbeater")
    
    // 构建Heartbeat的rpc客户端
    private val heartbeatReceiverRef = RpcUtils.makeDriverRef(HeartbeatReceiver.ENDPOINT_NAME, conf, env.rpcEnv)
    
    // 启动心跳定时线程
    private def startDriverHeartbeater(): Unit = {
        val heartbeatTask = new Runnable() {
            override def run(): Unit = Utils.logUncaughtExceptions(reportHeartBeat())
    	}
        heartbeater.scheduleAtFixedRate(heartbeatTask, initialDelay, intervalMs, TimeUnit.MILLISECONDS)
    }
    
    // 向driver汇报心跳
    private def reportHeartBeat(): Unit = {
        // 获取所有task运行的信息
        val accumUpdates = new ArrayBuffer[(Long, Seq[AccumulatorV2[_, _]])]()
        for (taskRunner <- runningTasks.values().asScala) {
            if (taskRunner.task != null) {
                accumUpdates += ((taskRunner.taskId, taskRunner.task.metrics.accumulators()))
            }
        }
        // 构建心跳消息
        val message = Heartbeat(executorId, accumUpdates.toArray, env.blockManager.blockManagerId)
        // 向driver发送
        val response = heartbeatReceiverRef.askSync[HeartbeatResponse](
          message, RpcTimeout(conf, "spark.executor.heartbeatInterval", "10s"))
        
}
```