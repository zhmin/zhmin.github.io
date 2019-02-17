---
title: Spark Streaming WAL 原理
date: 2019-02-17 21:29:24
tags: spark, streaming, wal
---

# Spark Streaming WAL 原理

WAL表示预写日志，经常在数据库中会使用到，在宕机后也能根据WAL恢复数据。Spark Streaming为了提高服务的容错性，也引入了WAL。它会将WAL存到可靠的文件系统 hdfs 里。Spark Streaming

运行在 executor 节点的 receiver， 从数据源读取数据后，如果配置了wal选项，会将数据写入WAL。这样当 executor 节点挂了之后，还能从WAL中恢复数据。

运行在 driver 节点的ReceivedBlockTracker，负责管理 block的元数据。当处理添加block，分配block和删除block请求的时候，会将此次事件信息写入WAL。



## WAL Writer 种类

WriteAheadLog是WAL处理的抽象类，由如下方法，提供了读取，写入，删除WAL。

```scala
public abstract class WriteAheadLog {
  
  // 将数据写入到WAL文件，参数record为即将保存的数据，参数time表示数据的结束时间
  // 返回WriteAheadLogRecordHandle对象，包含了存储的信息
  public abstract WriteAheadLogRecordHandle write(ByteBuffer record, long time);
  
  // 根据write方法返回的信息，来读取对应的数据
  public abstract ByteBuffer read(WriteAheadLogRecordHandle handle);

  // 读取所有还未过期的数据
  public abstract Iterator<ByteBuffer> readAll();

  // 清除过期的WAL文件，参数threshTime表示截止时间
  public abstract void clean(long threshTime, boolean waitForCompletion);
}
```

WriteAheadLog有两个子类，对应不同的存储原理。 

子类FileBasedWriteAheadLog，实现了以文件的方式存储数据。

子类BatchedWriteAheadLog，基于FileBasedWriteAheadLog之上，实现了批次的存储。



## FileBasedWriteAheadLog 原理

executor 节点上的WAL采用了FileBasedWriteAheadLog管理。如果要支持 wal，必须指定 checkpoint 的目录。

它的WAL目录格式如下：

```shell
checkpointDir/
├── receivedData
│   ├── streamId0
|   |   |── log-starttime0-endtime0
|   |   |── log-starttime1-endtime1
|   |   |── log-starttime1-endtime1
│   ├── streamId1
│   └── streamId2
```

wal的根目录是 checkpoint 目录下的 receivedData 目录。

每个 receiver 都有独立的目录，目录名为它的 id 号。

在每个独立的目录下，还会按照时间范围存储WAL文件，文件名中包含了起始时间和结束时间。 

### WAL创建

WAL的创建由write方法负责。再介绍write方法之前，需要先介绍FileBasedWriteAheadLogWriter类。它负责wal的写入，会将数据写入到 可靠的文件系统 hdfs 里。

```scala
class FileBasedWriteAheadLogWriter(path: String, hadoopConf: Configuration)
  extends Closeable {
  // 根据配置，返回hdfs的客户端或者本地文件的客户端，并且创建文件
  private lazy val stream = HdfsUtils.getOutputStream(path, hadoopConf)

  // 获取当前文件的偏移量
  private var nextOffset = stream.getPos()
  private var closed = false

  /** Write the bytebuffer to the log file */
  def write(data: ByteBuffer): FileBasedWriteAheadLogSegment = synchronized {
    data.rewind() // 准备读
    val lengthToWrite = data.remaining()
    // 记录本次数据的位置信息，WAL文件路径，起始位置，数据长度
    val segment = new FileBasedWriteAheadLogSegment(path, nextOffset, lengthToWrite)
    // 写入数据长度
    stream.writeInt(lengthToWrite)
    // 将bytebuffer的数据写入到outputstream
    Utils.writeByteBuffer(data, stream: OutputStream)
    // 刷新缓存
    flush()
    // 更新当前文件的偏移量
    nextOffset = stream.getPos()
    // 返回此次数据的信息
    segment
  }

  private def flush() {
    stream.hflush()
    // Useful for local file system where hflush/sync does not work (HADOOP-7844)
    stream.getWrappedStream.flush()
  }
}
```

FileBasedWriteAheadLogWriter的write方法返回FileBasedWriteAheadLogSegment结果，表示了此次WAL数据的位置信息。后面的WAL读取，会通过它来找到位置。

接下来看看FileBasedWriteAheadLog的write方法。它会根据时间范围，存储到不同的文件中。

```scala
private[streaming] class FileBasedWriteAheadLog {
  
  // 记录当前WAL文件的路径
  private var currentLogPath: Option[String] = None
  // 记录当前WAL writer
  private var currentLogWriter: FileBasedWriteAheadLogWriter = null
  // 当前WAL文件的开始时间
  private var currentLogWriterStartTime: Long = -1L
  // 当前WAL文件的结束时间
  private var currentLogWriterStopTime: Long = -1L
  // 记录完成的WAL文件信息
  private val pastLogs = new ArrayBuffer[LogInfo]
  
  def write(byteBuffer: ByteBuffer, time: Long): FileBasedWriteAheadLogSegment = synchronized {
    var fileSegment: FileBasedWriteAheadLogSegment = null
    var failures = 0
    var lastException: Exception = null
    var succeeded = false
    // 尝试最多maxFailures次数
    while (!succeeded && failures < maxFailures) {
      try {
        // 首先调用getLogWriter获取writer，
        // 然后通过writer写入数据
        fileSegment = getLogWriter(time).write(byteBuffer)
        if (closeFileAfterWrite) {
          resetWriter()
        }
        succeeded = true
      } catch {
        case ex: Exception =>
          lastException = ex
          logWarning("Failed to write to write ahead log")
          resetWriter()
          failures += 1
      }
    }
    if (fileSegment == null) {
      logError(s"Failed to write to write ahead log after $failures failures")
      throw lastException
    }
    fileSegment
  }
  
  private def getLogWriter(currentTime: Long): FileBasedWriteAheadLogWriter = synchronized {
    // 每个WAL文件都有对应的时间范围，如果超过了，则需要创建新的WAL文件
    if (currentLogWriter == null || currentTime > currentLogWriterStopTime) {
      // 关闭当前writer
      resetWriter()
      // 添加当前WAL的信息到currentLogPath列表，
      // 相关信息包括起始时间，结束时间，文件路径
      currentLogPath.foreach {
        pastLogs += LogInfo(currentLogWriterStartTime, currentLogWriterStopTime, _)
      }
      // 更新currentLogWriterStartTime为新的WAL文件的开始时间
      currentLogWriterStartTime = currentTime
      // 更新currentLogWriterStopTime为新的WAL文件的结束时间
      currentLogWriterStopTime = currentTime + (rollingIntervalSecs * 1000)
      // 生成新的WAL文件的路径
      val newLogPath = new Path(logDirectory,
        timeToLogFile(currentLogWriterStartTime, currentLogWriterStopTime))
      // 更新currentLogPath为新的WAL文件的路径
      currentLogPath = Some(newLogPath.toString)
      // 更新currentLogWriter为新的writer
      currentLogWriter = new FileBasedWriteAheadLogWriter(currentLogPath.get, hadoopConf)
    }
    currentLogWriter
  }  
```



### WAL 读取

WAL的读取由read方法负责，它只是通过FileBasedWriteAheadLogRandomReader来读取。FileBasedWriteAheadLogRandomReader支持seek操作，所以它支持单次数据的读取。

```scala
class FileBasedWriteAheadLog {  
  // 参数segment是 write方法返回的FileBasedWriteAheadLogSegment
  // 包含了数据的位置信息
  def read(segment: WriteAheadLogRecordHandle): ByteBuffer = {
    val fileSegment = segment.asInstanceOf[FileBasedWriteAheadLogSegment]
    var reader: FileBasedWriteAheadLogRandomReader = null
    var byteBuffer: ByteBuffer = null
    try {
      // 实例化FileBasedWriteAheadLogRandomReader，读取数据
      reader = new FileBasedWriteAheadLogRandomReader(fileSegment.path, hadoopConf)
      byteBuffer = reader.read(fileSegment)
    } finally {
      reader.close()
    }
    byteBuffer
  }
}

class FileBasedWriteAheadLogRandomReader(path: String, conf: Configuration)
  extends Closeable {
  // 获取hdfs的读客户端
  private val instream = HdfsUtils.getInputStream(path, conf)
  private var closed = (instream == null) // the file may be deleted as we're opening the stream

  def read(segment: FileBasedWriteAheadLogSegment): ByteBuffer = synchronized {
    // 获取该数据所在文件中的起始位置
    // 调用seek移动文件的读取位置
    instream.seek(segment.offset)
    // 读取数据的长度
    val nextLength = instream.readInt()
    // 实例化Byte数组
    val buffer = new Array[Byte](nextLength)
    // 读取数据到数组
    instream.readFully(buffer)
    // 返回ByteBuffer
    ByteBuffer.wrap(buffer)
  }
}
```



### WAL 删除

spark streaming处理的是流数据，它不可能会将所有的数据都保存下来。所以对于处理过的数据，它会定期删除掉。FileBasedWriteAheadLog同样提供了clean接口，来处理过期的数据

```scala
class FileBasedWriteAheadLog {
  // WAL文件信息列表
  private val pastLogs = new ArrayBuffer[LogInfo]
  
  def clean(threshTime: Long, waitForCompletion: Boolean): Unit = {
    // 找到结束时间小于threshTime的WAL文件
    val oldLogFiles = synchronized {
      val expiredLogs = pastLogs.filter { _.endTime < threshTime }
      pastLogs --= expiredLogs
      expiredLogs
    }
  
    def deleteFile(walInfo: LogInfo): Unit = {
      try {
        // 获取WAL文件的路径
        val path = new Path(walInfo.path)
        // 获取FileSystem
        val fs = HdfsUtils.getFileSystemForPath(path, hadoopConf)
        // 删除WAL文件
        fs.delete(path, true)
      } catch {
        case ex: Exception =>
          logWarning(s"Error clearing write ahead log file $walInfo", ex)
      }
    }
    
    // 遍历需要删除的WAL文件列表，调用deleteFile方法删除
    oldLogFiles.foreach { logInfo =>
      if (!executionContext.isShutdown) {
        try {
          // 使用线程池删除文件
          val f = Future { deleteFile(logInfo) }(executionContext)
          if (waitForCompletion) {
            import scala.concurrent.duration._
            // scalastyle:off awaitready
            Await.ready(f, 1 second)
            // scalastyle:on awaitready
          }
        } catch {
          case e: RejectedExecutionException =>
            logWarning("Execution context shutdown before deleting old WriteAheadLogs. " +
              "This would not affect recovery correctness.", e)
        }
      }
    }
  }
}
```



## BatchedWriteAheadLog

BatchedWriteAheadLog只运行在driver端，还需要spark配置中指定spark.streaming.driver.writeAheadLog.allowBatching选项为true。它会将数据线缓存起来，然后一次取多条数据，封装成一个批次存储到文件中。这样提高了系统的吞吐量。

BatchedWriteAheadLog首先将每次请求写入的数据，先缓存到一个队列。

```scala
class BatchedWriteAheadLog(val wrappedLog: WriteAheadLog, conf: SparkConf) {
  // WAL数据队列
  private val walWriteQueue = new LinkedBlockingQueue[Record]()
  
  override def write(byteBuffer: ByteBuffer, time: Long): WriteAheadLogRecordHandle = {
    val promise = Promise[WriteAheadLogRecordHandle]()
    val putSuccessfully = synchronized {
      if (active) {
        // 实例化Record，并且添加到walWriteQueue队列里
        walWriteQueue.offer(Record(byteBuffer, time, promise))
        true
      } else {
        false
      }
    }
    ......
  }    
}
```

BatchedWriteAheadLog还有一个后台线程，会一直从队列中获取数据，然后封装成batch的格式，通过FileBasedWriteAheadLog写入WAL。

```scala
class BatchedWriteAheadLog(val wrappedLog: WriteAheadLog, conf: SparkConf) {
  
  // WAL数据队列
  private val walWriteQueue = new LinkedBlockingQueue[Record]()
  // 数据缓存，保存即将保存的batch数据
  private val buffer = new ArrayBuffer[Record]()
  // batch write 线程
  private val batchedWriterThread = startBatchedWriterThread()
    
  private def startBatchedWriterThread(): Thread = {
    // 循环的调用flushRecords方法
    val thread = new Thread(new Runnable {
      override def run(): Unit = {
        while (active) {
          flushRecords()
        }
      }
    }, "BatchedWriteAheadLog Writer")
    thread.setDaemon(true)
    thread.start()
    thread
  }

  /** Write all the records in the buffer to the write ahead log. */
  private def flushRecords(): Unit = {
    try {
      // 这里调用take会阻塞，一直到队列中有数据
      buffer += walWriteQueue.take()
      // 调用drainTo能够高效率的导出数据
      val numBatched = walWriteQueue.drainTo(buffer.asJava) + 1
    } catch {
      case _: InterruptedException =>
        logWarning("BatchedWriteAheadLog Writer queue interrupted.")
    }
    try {
      var segment: WriteAheadLogRecordHandle = null
      if (buffer.length > 0) {
        // 依照时间排序
        val sortedByTime = buffer.sortBy(_.time)
        // 取WAL列表中最后的时间，作为batch的结束时间
        val time = sortedByTime.last.time
        // wrappedLog是FileBasedWriteAheadLog类型，在初始化的时候指定
        // aggregate方法将这一批次的数据，汇合到一个ByteBuffer里，
        // 然后通过FileBasedWriteAheadLog写入数据
        segment = wrappedLog.write(aggregate(sortedByTime), time)
      }
      buffer.foreach(_.promise.success(segment))
    } catch {
      ......
    } finally {
      // 清空buffer列表  
      buffer.clear()
    }
  }
}
```



因为BatchedWriteAheadLog的单位是batch，所以它不支持单次数据的读取。

```scala
override def read(segment: WriteAheadLogRecordHandle): ByteBuffer = {
  throw new UnsupportedOperationException("read() is not supported for BatchedWriteAheadLog " +
    "as the data may require de-aggregation.")
}

```



BatchedWriteAheadLog的删除操作，也只是转交给了FileBasedWriteAheadLog

```scala
override def clean(threshTime: Long, waitForCompletion: Boolean): Unit = {
  wrappedLog.clean(threshTime, waitForCompletion)
}
```

