---
title: Kafka 延迟任务
date: 2019-03-28 21:43:38
tags: kafka, timer, delay
categories: kafka
---

# Kafka 延迟任务



## 时间轮概念

Kafka在处理请求时，使用了多种延迟任务来处理，比如心跳请求。Kafka自己实现了时间轮，提供任务的延迟定时。关于时间轮的概念，我们把常见的钟表想象成一个时间轮。

时间轮都有着自己的定时范围，以时钟的时针为例，指针每前进一格，代表着时间过去一个小时。它最多有12格，能表示最大延迟时间不超过12个小时的任务。

时间轮可以分等级的，子时间轮的最大延迟时间，刚好为父时间轮的一格。类似于钟表一样。钟表有三个指针，时针，分针，秒针。分钟时间轮能表示的最大延迟时间为60分钟，刚好为小时时间轮的一格。

多级时间轮的精确时间是取决于最下层的时间轮。以钟表为例，它最下层的时间轮为秒针时间轮，精确的时间单位是秒。



## 任务列表

时间轮的每一格，都保存了对应时间的延迟任务列表。当向时间轮添加任务时，会根据任务的延迟时间，放到不同的时间轮里。比如当前时间是1点钟，现在添加一个延迟任务，它需要延迟1分钟1秒，添加的步骤如下：

1. 首先试图添加到秒时间轮里，但是秒时间轮最大的延迟时间是60秒，超过了最大延迟范围，所以会将任务尝试添加到分钟时间轮
2. 因为分钟时间轮的最大范围是60分钟，没有超过分钟时间轮的范围，所以任务添加到分钟时间轮的第二格。

当分针前进一格，到达1点1分，它会检测下一格的任务，会将该时间块的任务列表，取出来添加到秒时间轮。比如刚刚的延迟1分钟1秒的任务，会将它添加秒时间轮的二格。

当时间到达1点1分1秒，秒时间轮会执行该时间块的任务。



## 相关类介绍

TimerTask类表示延迟任务，继承Runnable，子类需要实现run方法。

TimerTaskEntry类表示链表项，它封装了TimerTask。

TimerTaskList表示延迟任务链表，它支持链表的添加和删除操作。它还提供了flush方法，支持执行延迟任务。

```scala
private[timer] class TimerTaskList(taskCounter: AtomicInteger) extends Delayed {
    
  private[this] val root = new TimerTaskEntry(null, -1)

  // 传递的函数，用来执行任务
  def flush(f: (TimerTaskEntry)=>Unit): Unit = {
    synchronized {
      // 遍历链表，执行延迟任务
      var head = root.next
      while (head ne root) {
        remove(head)
        f(head)
        head = root.next
      }
      expiration.set(-1L)
    }
  }
}
```



TimingWheel表示时间轮，它有wheelSize格时间块，每块的时间长度为tickMs。每块时间保存了TimerTaskList。

当添加任务时，任务超过了当前时间轮的范围，TimingWheel会自动创建父时间轮，直到父时间轮的范围可以包含此任务。

我们可以看到TimingWheel仍然使用了Java的DelayQueue类，实现定时作用。既然使用了DelayQueue，那为什么还会用到时间轮。根本原因就是性能，DelayQueue添加任务的复杂度是复杂度是O( n log(n) )，如果任务量太多，那么DelayQueue的性能会不好。Kafka引用了时间轮，使得添加任务的复杂度降低到了O（1），不过它将时间相差不大的任务，都添加到了一个队列里，这样就降低了时间精确性。

TimingWheel提供add方法添加任务，还提供了advanceClock方法更新时间。

```scala
// tickMs表示时间轮的时间单位，比如分钟时间轮的时间单位为1分钟
// wheelSize表示有多少格时间，比如分钟时间轮有60格
// startMs表示时间轮的开始时间
// queue是Java自带类DelayQueue类型，它只保存了任务队列
private[timer] class TimingWheel(tickMs: Long, wheelSize: Int, startMs: Long, taskCounter: AtomicInteger, queue: DelayQueue[TimerTaskList]) {
  // interval表示延迟的最大时间，比如分钟时间轮为60分钟
  private[this] val interval = tickMs * wheelSize
  // 每格时间都对应着一个任务队列
  private[this] val buckets = Array.tabulate[TimerTaskList](wheelSize) { _ => new TimerTaskList(taskCounter) }
  // 当前时间，它的时间单位为tickMs，这里采用向下取整
  private[this] var currentTime = startMs - (startMs % tickMs)
  // 父时间轮
  @volatile private[this] var overflowWheel: TimingWheel = null

  private[this] def addOverflowWheel(): Unit = {
    synchronized {
      if (overflowWheel == null) {
        overflowWheel = new TimingWheel(
          tickMs = interval,          // 父时间轮的时间单位，为当前时间轮的最大时间
          wheelSize = wheelSize,      // 父时间轮的格数相同
          startMs = currentTime,
          taskCounter = taskCounter,
          queue
        )
      }
    }
  }
    
  // 更新当前时间
  def advanceClock(timeMs: Long): Unit = {
    // 只有过了单位时间，才会更新当前时间
    if (timeMs >= currentTime + tickMs) {
      currentTime = timeMs - (timeMs % tickMs)
      // 更新父时间轮
      if (overflowWheel != null) overflowWheel.advanceClock(currentTime)
    }
  }
    
  // 添加任务，返回是否成功
  def add(timerTaskEntry: TimerTaskEntry): Boolean = {
    val expiration = timerTaskEntry.expirationMs
    if (timerTaskEntry.cancelled) {
      // 如果任务在添加之前，已经被取消掉了
      false
    } else if (expiration < currentTime + tickMs) {
      // 如果任务已经过期了
      false
    } else if (expiration < currentTime + interval) {
      // 计算添加到哪个时间格
      val virtualId = expiration / tickMs
      val bucket = buckets((virtualId % wheelSize.toLong).toInt)
      // 添加任务到对应的任务列表
      bucket.add(timerTaskEntry)

      // 设置任务列表的过期时间
      if (bucket.setExpiration(virtualId * tickMs)) {
        // 将列表添加到DepalyQueue
        queue.offer(bucket)
      }
      true
    } else {
      // 超出了最大延迟时间，需要添加到父时间轮
      if (overflowWheel == null) addOverflowWheel()
      overflowWheel.add(timerTaskEntry)
    }
  }    
}
```



## 定时器

时间轮只是保存了任务，而定时器负责管理时间轮和执行过期的任务。 定时器的接口由Timer表示

```scala
trait Timer {
  // 添加任务
  def add(timerTask: TimerTask): Unit

  // 更新时间，并且提交延迟任务给线程池执行
  def advanceClock(timeoutMs: Long): Boolean
}
```

SystemTimer实现了Timer接口，它使用DelayQueue查找过期的任务列表，然后提交线程池执行。

```scala
class SystemTimer(executorName: String,
                  tickMs: Long = 1,
                  wheelSize: Int = 20,
                  startMs: Long = Time.SYSTEM.hiResClockMs) extends Timer {

  // 执行任务的线程池
  private[this] val taskExecutor = Executors.newFixedThreadPool(1, new ThreadFactory() {
    def newThread(runnable: Runnable): Thread =
      KafkaThread.nonDaemon("executor-"+executorName, runnable)
  })
  // Java自带的延迟队列
  private[this] val delayQueue = new DelayQueue[TimerTaskList]()
  // 时间轮
  private[this] val timingWheel = new TimingWheel(
    tickMs = tickMs,
    wheelSize = wheelSize,
    startMs = startMs,
    taskCounter = taskCounter,
    delayQueue
  )
  
  // 添加任务
  def add(timerTask: TimerTask): Unit = {
    readLock.lock()
    try {
      addTimerTaskEntry(new TimerTaskEntry(timerTask, timerTask.delayMs + Time.SYSTEM.hiResClockMs))
    } finally {
      readLock.unlock()
    }
  }
  
    
  def advanceClock(timeoutMs: Long): Boolean = {
    // delayQueue查找过期的任务队列
    var bucket = delayQueue.poll(timeoutMs, TimeUnit.MILLISECONDS)
    if (bucket != null) {
      writeLock.lock()
      try {
        while (bucket != null) {
          // 调用时间轮的advanceClock方法，更新该时间轮的时间
          timingWheel.advanceClock(bucket.getExpiration())
          // 对任务列表依次执行reinsert操作
          bucket.flush(reinsert)
          // 继续查看是否还有超时的任务列表
          bucket = delayQueue.poll()
        }
      } finally {
        writeLock.unlock()
      }
      true
    } else {
      false
    }
  }
  
  private[this] val reinsert = (timerTaskEntry: TimerTaskEntry) => addTimerTaskEntry(timerTaskEntry)
  
  private def addTimerTaskEntry(timerTaskEntry: TimerTaskEntry): Unit = {
    
    if (!timingWheel.add(timerTaskEntry)) {
      // 如果是因为任务过期，导致添加失败，那么将任务丢到线程池执行
      if (!timerTaskEntry.cancelled)
        taskExecutor.submit(timerTaskEntry.timerTask)
    }
  }
  
```



## 延迟任务

DelayedOperation表示延迟任务，它在TimerTask的基础上，提供了支持提前完成的功能。

子类需要实现DelayedOperation的两个重要回调，onComplete 和 onExpire，对应着不同情形的回调

- 当任务提前完成时，只会调用onComplete方法。
- 当任务因为到期才执行，会调用onComplete方法和onExpire方法

DelayedOperation提供了tryComplete方法，供使用者调用，来尝试提前完成任务。子类需要实现这个方法，判断是否满足提前完成条件，如果满足则执行forceComplete方法执行任务。

```scala
abstract class DelayedOperation {
  
  // 这里使用了AtomicBoolean，用来控制并发
  private val completed = new AtomicBoolean(false)
    
  // 执行任务
  def forceComplete(): Boolean = {
    if (completed.compareAndSet(false, true)) {
      // 尝试设置completed的值
      // 从定时器中取消任务
      cancel()
      // 调用onComplete方法，执行回调
      onComplete()
      true
    } else {
      false
    }
  }    
}
```

DelayedOperation还提供了maybeTryComplete方法，在tryComplete方法的基础之上，提供了多线程的优化。maybeTryComplete方法实现得很精巧，它能保证尽量及时的检测任务是否可以完成。

```scala
// 是否需要再次尝试
private val tryCompletePending = new AtomicBoolean(false)

private[server] def maybeTryComplete(): Boolean = {
  var retry = false
  var done = false
  do {
    if (lock.tryLock()) {
      // 成功获取锁
      try {
        // 设置tryCompletePending为false，表示不再尝试
        tryCompletePending.set(false)
        
        done = tryComplete()
      } finally {
        lock.unlock()
      }
      // 获取tryCompletePending的值，有可能此时外部线程修改了值
      retry = tryCompletePending.get()
    } else {
      // 如果获取锁失败，设置tryCompletePending为true，通知获取锁的线程再次尝试。
      // 如果tryCompletePending之前为false，表示获取所的线程尝试操作已完成，不能保证。获取所失败的线程，需要自己尝试
      // 如果tryCompletePending之前为true，表示现在已有一个获取锁失败的线程在运行，所以当前线程不用再尝试
      retry = !tryCompletePending.getAndSet(true)
      
    }
  } while (!isCompleted && retry)
  done
}
```

maybeTryComplete方法实现得很精巧，它能保证尽量及时的检测任务是否可以完成。如果线程A首先获取锁，但是这时没有满足条件。之后线程B获取锁失败，但是此时说不定满足条件，所以这里需要再次检查条件，至于是哪个线程执行都可以。

## 延迟任务管理

DelayedOperationPurgatory负责管理延迟任务，支持任务分组。分组信息由Watchers类表示，它包含了延迟任务列表和任务类型。Watchers提供了tryCompleteWatched方法，会尝试完成列表中的任务。

```scala
// key为任务类型
private class Watchers(val key: Any) {
    // 延迟任务列表
    private[this] val operations = new ConcurrentLinkedQueue[T]()
    
    def tryCompleteWatched(): Int = {
      var completed = 0
      // 遍历任务列表
      val iter = operations.iterator()
      while (iter.hasNext) {
        val curr = iter.next()
        if (curr.isCompleted) {
          // another thread has completed this operation, just remove it
          iter.remove()
        } else if (curr.maybeTryComplete()) {
          // 调用DelayedOperation的maybeTryComplete方法，尝试完成任务
          iter.remove()
          completed += 1
        }
      }

      if (operations.isEmpty)
        // Watchers是DelayedOperationPurgatory的内部类，这里的removeKeyIfEmpty是属于DelayedOperationPurgatory类的方法
        // 如果当前列表的任务都已经完成，那么将这个分组删除掉
        removeKeyIfEmpty(key, this)
      completed
    }
}
```



DelayedOperationPurgatory里包含了一个线程，用来更新时间轮的时间，并且执行过期任务。

```scala
final class DelayedOperationPurgatory[T <: DelayedOperation](...) {
  // 更新时间的线程
  private val expirationReaper = new ExpiredOperationReaper()
  
  private class ExpiredOperationReaper extends ShutdownableThread(
    "ExpirationReaper-%d-%s".format(brokerId, purgatoryName),
    false) {
      
    // 该线程调用DelayedOperationPurgatory的advanceClock方法，更新时间轮
    override def doWork() {
      advanceClock(200L)
    }
  }
  
  // timeoutMs参数，表示此次操作的超时时间
  def advanceClock(timeoutMs: Long) {
    // 调用SystemTimer的advanceClock方法，执行过期的任务
    timeoutTimer.advanceClock(timeoutMs)
    // estimatedTotalOperations表示 DelayedOperationPurgatory的任务数，包含已经完成的任务
    // delayed表示时间轮还未完成的任务数
    if (estimatedTotalOperations.get - delayed > purgeInterval) {
      estimatedTotalOperations.getAndSet(delayed)
      // 遍历Watchers列表，清除已经完成的任务
      val purged = allWatchers.map(_.purgeCompleted()).sum
    }
  }  
  
}
```



DelayedOperationPurgatory提供了两个重要方法，供外部使用。使用者首先调用tryCompleteElseWatch方法，添加延迟任务。添加完后，不定时的调用checkAndComplete方法，尝试提前完成任务。

- tryCompleteElseWatch方法，提供添加延迟任务
- checkAndComplete方法，负责尝试提前完成任务

```scala
final class DelayedOperationPurgatory[T <: DelayedOperation] (...) {

  private val watchersForKey = new Pool[Any, Watchers](Some((key: Any) => new Watchers(key)))

  // 注意到watchKeys是一个列表，延迟任务与事件类型是多对多的关系
  def tryCompleteElseWatch(operation: T, watchKeys: Seq[Any]): Boolean = {
    // 尝试提前完成任务
    var isCompletedByMe = operation.tryComplete()
    if (isCompletedByMe)
      return true

    var watchCreated = false
    for(key <- watchKeys) {
      // 有可能别的线程，提前完成了任务
      if (operation.isCompleted)
        return false
      // 将任务添加到key对应的列表
      watchForOperation(key, operation)

      if (!watchCreated) {
        watchCreated = true
        estimatedTotalOperations.incrementAndGet()
      }
    }
      
    // 再次尝试完成任务
    isCompletedByMe = operation.maybeTryComplete()
    if (isCompletedByMe)
      return true

    if (!operation.isCompleted) {
      if (timerEnabled)
        // 添加到定时器中
        timeoutTimer.add(operation)
      if (operation.isCompleted) {
        // 如果这时候任务已经完成，那么就取消任务
        operation.cancel()
      }
    }
    false
  }

  def checkAndComplete(key: Any): Int = {
    // 获取该事件类型的任务列表
    val watchers = inReadLock(removeWatchersLock) { watchersForKey.get(key) }
    if(watchers == null)
      0
    else
      // 调用Watchers的tryCompleteWatched方法，尝试提前完成任务
      watchers.tryCompleteWatched()
  }  
}
```

