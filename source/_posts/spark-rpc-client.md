---
title: Spark Rpc 客户端原理
date: 2019-01-13 21:34:54
tags: spark, rpc, client
categories: spark
---

# Spark Rpc 客户端原理 #

Spark Rpc 客户端涉及到多个组件，根据发送消息和接收消息的流程，逐一介绍这些组件。可以参见流程图 {% post_link  spark-rpc-flow %} 

 

## RpcEndpointRef ##

RpcEndpointRef表示客户端，它提供了两个接口发送请求。send方法表示发送请求但不需要返回值，ask方法表示发送请求并且需要获取返回值。

RpcEndpointRef目前只有一个实现类NettyRpcEndpointRef，基于Netty框架实现的。

NettyRpcEndpointRef有两个比较重要的属性，

* endpointAddress， 请求的server地址
* NettyRpcEnv实例，表示rpc的运行环境



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

NettyRpcEndpointRef的send和ask方法，都是转交给了NettyRpcEnv发送。



## 客户端的NettyRpcEnv  ##

所有的rpc客户端和服务，都是在NettyRpcEnv环境下才能运行。NettyRpcEnv有比较多的属性，涉及到客户端的属性，主要如下

* outboxes， 表示Outbox集合。每个Outbox对应着一个server的地址( 主机地址， 端口号)
* address， 表示server运行的监听地址。如果该NettyRpcEnv中没有运行的server，则为null

* dispatcher， 将请求消息分发给对应的inbox。只有当客户端和server运行在同一个NettyRpcEnv，才会调用dispatcher直接发送。否则都需要建立socket连接

首先看看NettyRpcEnv的send方法，

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
      // 这里序列化了消息，并生成OneWayOutboxMessage消息
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
          // 这里调用了putIfAbsent，防止线程竞争
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



继续看ask方法， ask方法需要返回server的返回值，它使用了异步请求。这里使用了promise，当请求完成时，会将结果保存在promise里。通过访问promise的Future就可以获取结果。

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
      // 生成RpcOutboxMessage消息 
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
        onFailure(new TimeoutException(s"Cannot receive any reply from ${remoteAddr} "
                                       + s"in ${timeout.duration}"))
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

Outbox接收两种消息，OneWayOutboxMessage和RpcOutboxMessage。两个都是继承OutboxMessage类。

OneWayOutboxMessage表示，请求不需要返回结果。RpcOutboxMessage表示，需要返回结果。



### OneWayOutboxMessage

OneWayOutboxMessage提供了sendWith方法，将消息发送出去。这里只是直接调用TransportClient的send方法发送。

OneWayOutboxMessage定义了请求失败的回调函数onFailure

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



### RpcOutboxMessage 

OneWayOutboxMessage提供了sendWith方法，将消息发送出去。这里只是直接调用TransportClient的sendRpc方法发送。

OneWayOutboxMessage定义了三个回调函数

* onTimeout代表着超时，在NettyRpcEnv的ask方法有实现。
* onSuccess代表着请求成功返回时，会被调用。
* onFailure代表着失败，当请求失败时，Outbox会调用这个方法。



```scala
class RpcOutboxMessage(
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



### 发送消息 ###

一个Outbox对应着一个server地址。Outbox作为一个消息队列，它提供了send方法添加消息，也提供了drainOutbox消费消息。

Outbox有下列主要属性：

* messages， 消息队列
* client， TransportClient实例，它作为Netty的客户端，异步发送消息
* connectFuture， 表示新建server连接的异步结果
* draining， 表示是否有线程正在发送消息。Outbox允许同时只有一个线程发送消息，所以在发送消息之前，都会判断draining的值



首先来看send方法，它的源码比较简单。首先它会将消息添加到队列里，然后调用drainOutbox发送消息

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



再来看看drainOutbox方法。这里涉及到与server建立连接，还有多线程竞争， 会有点复杂。

它首先会判断是否建立server的连接，如果没有连接，则请求后台线程创建连接。如果有，则直接发送。注意到，这里发送消息，有线程冲突。因为后台线程创建完连接后，它也会调用drainOutbox发送消息。

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
        // draining为true，表示有别的线程正在发送消息
        return
      }
      message = messages.poll()
      // messages.poll返回null，表示消息队列都已经发送完成
      if (message == null) {
        return
      }
      // 更新draining为true， 表示消费消息的权利
      draining = true
    }
    while (true) {
      try {
        val _client = synchronized { client }
        if (_client != null) {
          // 调用消息的sendWith方法，发送消息
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
        // 循环从队列获取消息， 直到所有的消息发送完成
        message = messages.poll()
        if (message == null) {
          // 释放消费消息的权利
          draining = false
          return
        }
      }
    }
  }
```



### 创建连接

使用后台线程池连接，这个线程池的大小为配置项spark.rpc.connect.threads，默认60。超时时间为60s。

注意后台线程创建完连接后，它会主动处理队列的消息，防止消息堆积。如果它不主动发送消息，则只能等待客户的下一次消息的发送，而这个假定是不确定的。

```scala
  private def launchConnectTask(): Unit = {
    // 提交连接任务给，nettyEnv的的线程池
    connectFuture = nettyEnv.clientConnectionExecutor.submit(new Callable[Unit] {

      override def call(): Unit = {
        try {
          // 实例化TransportClient
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



## TransportClient ##

从RpcOutboxMessage的sendWith源码，可以看到消息是调用TransportClient的sendRpc或send方法发送。

send方法是发送消息，但不需要server的返回值。这里仅仅是调用了channel的writeAndFlush方法

```scala
public void send(ByteBuffer message) {
  channel.writeAndFlush(new OneWayMessage(new NioManagedBuffer(message)));
}
```



sendRpc方法是是需要处理server的返回值的。

```java
public class TransportClient implements Closeable {
    
    private final Channel channel;
    private final TransportResponseHandler handler;
    
    public long sendRpc(ByteBuffer message, RpcResponseCallback callback) {
      // 为此次请求分配requestId
      long requestId = Math.abs(UUID.randomUUID().getLeastSignificantBits());
      // 注意这里将请求信息添加到TransportResponseHandler里，
      // TransportResponseHandler会在响应完成时，调用此次响应完成的回调函数callback
      handler.addRpcRequest(requestId, callback);

      channel.writeAndFlush(new RpcRequest(requestId, new NioManagedBuffer(message)))
          .addListener(future -> {
            if (future.isSuccess()) {
                ...
            } else {
              // 处理请求失败的情况
              handler.removeRpcRequest(requestId);
              channel.close();
              try {
                callback.onFailure(new IOException(errorMsg, future.cause()));
              } catch (Exception e) {
                logger.error("Uncaught exception in RPC response callback handler!", e);
              }
            }
          });

      return requestId;
    }
}
```



TransportClient是基于Netty框架的，每个TransportClient有着一个Netty的Channel实例，通过它发送数据。接下来先看看它是如何初始化Netty的。

TransportClientFactory提供了实例化TransportClient的方法

```java
public class TransportClientFactory implements Closeable {
    
     TransportClient createClient(InetSocketAddress address)
          throws IOException, InterruptedException {
       
        // 使用Netty的Bootstrap初始化Client
        Bootstrap bootstrap = new Bootstrap();
        bootstrap.group(workerGroup)
          .channel(socketChannelClass)
          .option(ChannelOption.TCP_NODELAY, true)
          .option(ChannelOption.SO_KEEPALIVE, true)
          .option(ChannelOption.CONNECT_TIMEOUT_MILLIS, conf.connectionTimeoutMs())
          .option(ChannelOption.ALLOCATOR, pooledAllocator);

        final AtomicReference<TransportClient> clientRef = new AtomicReference<>();
        final AtomicReference<Channel> channelRef = new AtomicReference<>();
        
        // 初始化ChannelHandler
        bootstrap.handler(new ChannelInitializer<SocketChannel>() {
          @Override
          public void initChannel(SocketChannel ch) {
            // 调用了TransportContext的initializePipeline
            TransportChannelHandler clientHandler = context.initializePipeline(ch);
            clientRef.set(clientHandler.getClient());
            channelRef.set(ch);
          }
        });

        // 连接到服务器
        long preConnect = System.nanoTime();
        ChannelFuture cf = bootstrap.connect(address);

         // 执行连接后的动作，比如身份权限验证
        for (TransportClientBootstrap clientBootstrap : clientBootstraps) {
             clientBootstrap.doBootstrap(client, channel);
        }

        return client;
      }
```



createClient方法通过Bootstrap，初始化了Netty的客户端。注意到最主要的，调用了TransportContext的initializePipeline来配置Channel Pipeline。

```java
public class TransportContext {
    
  private final RpcHandler rpcHandler;
    
  public TransportChannelHandler initializePipeline(SocketChannel channel) {
    return initializePipeline(channel, rpcHandler);
  }
  
  public TransportChannelHandler initializePipeline(
      SocketChannel channel,
      RpcHandler channelRpcHandler) {
    
    try {
      // 创建 channelHandler
      TransportChannelHandler channelHandler = createChannelHandler(channel, channelRpcHandler);
      channel.pipeline()
        .addLast("encoder", ENCODER)
        .addLast(TransportFrameDecoder.HANDLER_NAME, NettyUtils.createFrameDecoder())
        .addLast("decoder", DECODER)
        .addLast("idleStateHandler", new IdleStateHandler(0, 0, conf.connectionTimeoutMs() / 1000))
        // 添加 TransportChannelHandler
        .addLast("handler", channelHandler);
      return channelHandler;
    } catch (RuntimeException e) {
      logger.error("Error while initializing Netty pipeline", e);
      throw e;
    }
  }
}
```



注意上面的TransportChannelHandler类，它继承ChannelInboundHandlerAdapter

```java
public class TransportChannelHandler extends ChannelInboundHandlerAdapter {
  
  @Override
  public void channelRead(ChannelHandlerContext ctx, Object request) throws Exception {
    if (request instanceof RequestMessage) {
      requestHandler.handle((RequestMessage) request);
    } else if (request instanceof ResponseMessage) {
      responseHandler.handle((ResponseMessage) request);
    } else {
      ctx.fireChannelRead(request);
    }
  }
}
```

TransportChannelHandler在接收响应时，会触发channelRead函数。这里调用TransportResponseHandler的handle函数。

```java
public class TransportResponseHandler extends MessageHandler<ResponseMessage> {
  @Override
  public void handle(ResponseMessage message) throws Exception {
    if (message instanceof RpcResponse) {
      RpcResponse resp = (RpcResponse) message;
      // 根据requestId，取出对应的回调函数
      RpcResponseCallback listener = outstandingRpcs.get(resp.requestId);
      
      ...... 
          
      if (listener == null) {
          ......
      } else {
        // 调用onSuccess回调，参数为响应数据
        outstandingRpcs.remove(resp.requestId);
        try {
          listener.onSuccess(resp.body().nioByteBuffer());
        } finally {
          resp.body().release();
        }
      }
    }
  }
}
```




