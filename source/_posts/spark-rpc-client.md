---
title: spark-rpc-client
date: 2019-01-13 21:34:54
tags: spark, rpc, client
---

# Spark Rpc 客户端原理 #

Spark Rpc 客户端涉及到多个组件，可以分为发送消息和接收消息两块。



## RpcEndpointRef ##

RpcEndpointRef表示客户端，它提供了两个接口，send方法表示发送请求但不需要返回值。ask方法表示发送请求并且需要获取返回值。

RpcEndpointRef目前只有一个实现类NettyRpcEndpointRef，基于Netty框架实现的。

NettyRpcEndpointRef有两个比较重要的属性，

* 服务器地址，endpointAddress
* TransportClient实例，如果为null表示这个RpcEndpointRef是请求远端服务。否则表示服务端与客户端是同一个进程内
* NettyEnv实例，表示rpc的运行环境



```scala
class NettyRpcEndpointRef(
    @transient private val conf: SparkConf,
    private val endpointAddress: RpcEndpointAddress,
    @transient @volatile private var nettyEnv: NettyRpcEnv) extends RpcEndpointRef(conf) {
  
  // 调用NettyRpcEnv的ask方法发送请求
  override def ask[T: ClassTag](message: Any, timeout: RpcTimeout): Future[T] = {
    nettyEnv.ask(new RequestMessage(nettyEnv.address, this, message), timeout)
  }

  // 调用NettyRpcEnv的send方法发送请求
  override def send(message: Any): Unit = {
    require(message != null, "Message is null")
    nettyEnv.send(new RequestMessage(nettyEnv.address, this, message))
  }
}
```



## 客户端的NettyRpcEnv  ##

所有的rpc客户端或者服务，都是在NettyRpcEnv环境下才能运行。NettyRpcEnv有比较多的属性，涉及到客户端的属性，主要如下

* outboxes， 表示Outbox集合。每个Outbox对应着一个server的地址( 主机地址， 端口号)
* address， 表示server运行绑定的地址。如果该NettyRpcEnv中没有运行server，则为null



首先看看send方法，

```scala
class NettyRpcEnv {
    
  // OutBox集合，Key为server的地址
  private val outboxes = new ConcurrentHashMap[RpcAddress, Outbox]()
    
  private[netty] def send(message: RequestMessage): Unit = {
    val remoteAddr = message.receiver.address
    if (remoteAddr == address) {
      // 如果要请求的地址，是正在同一个NettyRpcEnv运行的server,
     // 则直接通过dispatcher转发，而不需要建立socket连接
      try {
        dispatcher.postOneWayMessage(message)
      } catch {
        case e: RpcEnvStoppedException => logWarning(e.getMessage)
      }
    } else {
      // 如果是远端server，则调用postToOutbox方法发送
      // 这里序列化了消息
      postToOutbox(message.receiver, OneWayOutboxMessage(message.serialize(this)))
    }
  }
    
  // 通过Outbox发送消息
  private def postToOutbox(receiver: NettyRpcEndpointRef, message: OutboxMessage): Unit = {
    // NettyRpcEndpointRef的client不为空
    if (receiver.client != null) {
      message.sendWith(receiver.client)
    } else {
      require(receiver.address != null,
        "Cannot send message to client endpoint with no listen address.")
     // 根据server地址，寻找Outbox。如果没找到，则新建
      val targetOutbox = {
        // 从Outbox集合寻找
        val outbox = outboxes.get(receiver.address)
        if (outbox == null) {
          // 新建Outbox
          val newOutbox = new Outbox(this, receiver.address)
          val oldOutbox = outboxes.putIfAbsent(receiver.address, newOutbox)
          if (oldOutbox == null) {
            newOutbox
          } else {
            oldOutbox
          }
        } else {
          outbox
        }
      }
      // stopped属性表示rpc服务是否已经关闭
      if (stopped.get) {
        // It's possible that we put `targetOutbox` after stopping. So we need to clean it.
        outboxes.remove(receiver.address)
        targetOutbox.stop()
      } else {
        // 调用Outbox发送消息
        targetOutbox.send(message)
      }
    }
  }
}
```

继续看ask方法， ask方法因为需要处理server的返回值，所以它定义了处理响应的回调函数。ask返回了Promise实例，在收到响应后，会设置它的值。

```scala
def ask[T: ClassTag](message: RequestMessage, timeout: RpcTimeout): Future[T] = {
  val promise = Promise[Any]()
  val remoteAddr = message.receiver.address

  // server返回失败时的回调函数
  // 设置promise的结果为失败
  def onFailure(e: Throwable): Unit = {
    if (!promise.tryFailure(e)) {
      logWarning(s"Ignored failure: $e")
    }
  }

  // server返回成功时的回调函数
  // 设置promise的结果为成功，数据为server的返回值
  def onSuccess(reply: Any): Unit = reply match {
    case RpcFailure(e) => onFailure(e)
    case rpcReply =>
      if (!promise.trySuccess(rpcReply)) {
        logWarning(s"Ignored message: $reply")
      }
  }

  try {
    // 如果client和server是同一个进程内
    if (remoteAddr == address) {
      val p = Promise[Any]()
      p.future.onComplete {
        case Success(response) => onSuccess(response)
        case Failure(e) => onFailure(e)
      }(ThreadUtils.sameThread)
      // 直接调用dispatcher转发消息
      dispatcher.postLocalMessage(message, p)
    } else {
      // 设置RpcOutboxMessage消息的回调函数
      val rpcMessage = RpcOutboxMessage(message.serialize(this),
        onFailure,
        (client, response) => onSuccess(deserialize[Any](client, response)))
      // 通过Outbox发送消息
      postToOutbox(message.receiver, rpcMessage)
      promise.future.onFailure {
        case _: TimeoutException => rpcMessage.onTimeout()
        case _ =>
      }(ThreadUtils.sameThread)
    }

    // 设置超时机制，如果超时还没接收响应，则调用onFailure函数
    val timeoutCancelable = timeoutScheduler.schedule(new Runnable {
      override def run(): Unit = {
        onFailure(new TimeoutException(s"Cannot receive any reply from ${remoteAddr} " +
          s"in ${timeout.duration}"))
      }
    }, timeout.duration.toNanos, TimeUnit.NANOSECONDS)
    
    // 如果在超时前，完成响应，则取消这个请求的超时
    promise.future.onComplete { v =>
      timeoutCancelable.cancel(true)
    }(ThreadUtils.sameThread)
  } catch {
    case NonFatal(e) =>
      onFailure(e)
  }
  // 等待响应完成，等待时间不超过timeout
  promise.future.mapTo[T].recover(timeout.addMessageIfTimeout)(ThreadUtils.sameThread)
}
```



## OutBox ##



Outbox提供了send方法，给调用方发送消息。从send方法的申明可以看到，它只接受OutboxMessage类型的消息。OutboxMessage表示发送消息，它有两个子类，OneWayOutboxMessage和RpcOutboxMessage。OneWayOutboxMessage表示，请求不需要返回结果。RpcOutboxMessage表示，需要返回结果。



### OneWayOutboxMessage

```scala
private[netty] case class OneWayOutboxMessage(content: ByteBuffer) extends OutboxMessage
  with Logging {

  override def sendWith(client: TransportClient): Unit = {
    client.send(content)
  }

  override def onFailure(e: Throwable): Unit = {
    e match {
      case e1: RpcEnvStoppedException => logWarning(e1.getMessage)
      case e1: Throwable => logWarning(s"Failed to send one-way RPC.", e1)
    }
  }

}
```

发送OneWayOutboxMessage，就直接调用TransportClient的send方法发送出去。



### RpcOutboxMessage

```scala
private[netty] case class RpcOutboxMessage(
    content: ByteBuffer,
    _onFailure: (Throwable) => Unit,
    _onSuccess: (TransportClient, ByteBuffer) => Unit)
  extends OutboxMessage with RpcResponseCallback with Logging {
        private var client: TransportClient = _
  private var requestId: Long = _

  override def sendWith(client: TransportClient): Unit = {
    this.client = client
    this.requestId = client.sendRpc(content, this)
  }
      
  def onTimeout(): Unit = {
    if (client != null) {
      client.removeRpcRequest(requestId)
    } else {
      logError("Ask timeout before connecting successfully")
    }
  }
      
  override def onFailure(e: Throwable): Unit = {
    _onFailure(e)
  }

  override def onSuccess(response: ByteBuffer): Unit = {
    _onSuccess(client, response)
  }
```

发送RpcOutboxMessage，首先保存了TransportClient， 然后将自身作为回调对象，通过TransportClient的sendRpc发送出去。

这里有三个回调函数，onTimeout代表着超时，这里超时功能的实现，是由NettyRpcEnv的timeoutScheduler实现的。

onSuccess代表着成功返回，是在TransportResponseHandler中，会被调用。

onFailure代表着失败，当连接失败时，Outbox会调用这个方法。





Outbox 代表着发送者，一个Outbox对应着一个服务。请求该服务的消息，都要先发送到，然后由Outbox发送出去。Outbox管理着与服务的通信和发送的消息队列。



* messages， 消息队列
* client， TransportClient实例
* connectFuture， 表示新建server连接的异步结果
* draining



```scala
def send(message: OutboxMessage): Unit = {
  // 如果rpc服务停止，则表示此条消息不能发送，需要丢弃
  val dropped = synchronized {
    if (stopped) {
      true
    } else {
      messages.add(message)
      false
    }
  }
  // 如果此条消息被舍弃，调用message的onFailure回调函数
  if (dropped) {
    message.onFailure(new SparkException("Message is dropped because Outbox is stopped"))
  } else {
    // drainOutbox定义了如何发送队列里的消息
    drainOutbox()
  }
}
```



drainOutbox

首先是否已经建立server的连接，如果没有连接，则请求后台线程创建连接。如果有，则直接发送。注意到，这里发送消息，有线程冲突。因为后台线程创建完连接后，它会主动尝试发送队列里的消息。

```scala
  private def drainOutbox(): Unit = {
    
    var message: OutboxMessage = null
     // 锁，防止发送消息的主线程，和创建连接的线程有冲突
    synchronized {
      if (stopped) {
        return
      }
      if (connectFuture != null) {
        // connectFuture不为null，表示正在创建连接中，但未完成
        return
      }
      if (client == null) {
        // 如果connectFuture为null，client也为null，表示没有连接
        // 所以这儿提交创建新连接的任务
        launchConnectTask()
        return
      }
      if (draining) {
        // draining为true，表示已有线程正在发送消息
        return
      }
      message = messages.poll()
      // messages.poll返回null，表示消息队列都已经发送完成
      if (message == null) {
        return
      }
      // 更新draiing为true
      draining = true
    }
    while (true) {
      try {
        val _client = synchronized { client }
        if (_client != null) {
          // 发送消息
          message.sendWith(_client)
        } else {
          assert(stopped == true)
        }
      } catch {
        case NonFatal(e) =>
          handleNetworkFailure(e)
          return
      }
      synchronized {
        if (stopped) {
          return
        }
        // 循环从队列获取消息
        message = messages.poll()
        if (message == null) {
          // 知道所有的消息发送完成
          draining = false
          return
        }
      }
    }
  }
```

### 创建连接

使用后台线程池连接，这个线程池的大小为配置项spark.rpc.connect.threads，默认60。超时时间为60s。

注意后台线程创建完连接后，它会主动处理队列的消息，防止消息堆积。如果它不主动发送消息，则只能等待客户的下一次发送消息，而这个假定是未知的。

```scala
  private def launchConnectTask(): Unit = {
    // 提交连接任务给，nettyEnv的的线程池
    connectFuture = nettyEnv.clientConnectionExecutor.submit(new Callable[Unit] {

      override def call(): Unit = {
        try {
          val _client = nettyEnv.createClient(address)
          outbox.synchronized {
            
            client = _client
            if (stopped) {
              closeClient()
            }
          }
        } catch {
          case ie: InterruptedException =>
            // exit
            return
          case NonFatal(e) =>
            outbox.synchronized { connectFuture = null }
            handleNetworkFailure(e)
            return
        }
        outbox.synchronized { connectFuture = null }
        // 连接完成后，处理堆积的消息
        drainOutbox()
      }
    })
  }
```

### 