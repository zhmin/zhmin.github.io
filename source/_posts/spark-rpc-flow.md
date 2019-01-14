---
title: Spark Rpc 原理介绍
date: 2019-01-13 14:05:40
tags: spark, rpc
---

# Spark Rpc 原理介绍 #

Spark的Rpc服务，是整个Spark框架的基石。Spark的很多服务都是基于Rpc框架之上的，它承担了各个服务之间的信息交流。下面是Rpc的各个组件运行的流程图 :

## rpc客户端发送消息

<img src="rpc-client-send.svg">

RpcEndpointRef ： Rpc客户端，通过它可以发送消息

NettyRpcEnv ： 整个Rpc的运行环境，RpcEndpointRef是通过NettyRpcEnv才能把消息发送出去

OutBox ： 消息发件箱，存储消息队列

Thread Pool ： 负责TrasnportClient初始化的线程池。TrasnportClient初始化的时候，会和服务端新建连接

TrasnportClient ： Netty客户端，负责与服务端交互



## rpc客户端接收消息

<img src="rpc-client-receive.svg">

TrasnportClient ： Netty客户端，负责与服务端交互

EventLoopGroup : Netty客户端的工作线程池，当收到server的消息时，会触发回调函数

RpcMessage : rpc客户端发送的消息，里面包含了回调函数





## rpc服务端处理请求

<img src="rpc-server-receive.svg">

TrasnportServer： Netty服务端，负责接收消息

EventLoopGroup : Netty客户端的工作线程池，当收到client的请求时，会处理请求

Dispatcher : 分发器，将请求分发给对应的Inbox

Inbox ： 收件箱，每个RpcEndpoint都有独立的收件箱，存储着请求

RpcEndpoint ： Rpc服务端，它定义了处理请求的逻辑



## rpc服务端发送响应

<img src="rpc-server-send.svg">

RpcEndpoint ： Rpc服务端，它定义了处理请求的逻辑

Channel : SocketChannel类，负责传输数据

Netty : 通过Netty发送响应

Client ： 请求端



## 测试demo ##



### rpc服务端 ###

这里定义了HelloEndpoint服务。对于请求SayHi，响应 hello。

```scala
package org.apache.spark.rpc.HelloEndpoint

import org.apache.spark.SparkConf
import org.apache.spark.rpc.{RpcCallContext, RpcEndpoint, RpcEnv}
import org.apache.spark.SecurityManager


class HelloEndpoint(override val rpcEnv: RpcEnv) extends RpcEndpoint {
  override def onStart(): Unit = {
    println("start hello endpoint")
  }

  override def receiveAndReply(context: RpcCallContext): PartialFunction[Any, Unit] = {
    case SayHello(msg) => {
      println(s"receive $msg")
      context.reply(s"hello, $msg")
    }
  }

  override def onStop(): Unit = {
    println("stop hello endpoint")
  }
}

case class SayHello(msg: String)

object HelloEndpoint {
  def main(args: Array[String]): Unit = {
    val conf = new SparkConf()
    val manager = new SecurityManager(conf)
    // 创建server模式的RpcEnv
    val rpcEnv: RpcEnv = RpcEnv.create("hello-server", "localhost", 5432, conf, manager)
    // 实例化HelloEndpoint
    val helloEndpoint: RpcEndpoint = new HelloEndpoint(rpcEnv)
    // 在RpcEnv注册helloEndpoint
    rpcEnv.setupEndpoint("hello-service", helloEndpoint)
    // 等待线程rpcEnv运行完
    rpcEnv.awaitTermination()
  }
}
```



### rpc客户端 ###

```scala
package org.apache.spark.rpc.netty.HelloEndpointRed

import org.apache.spark.{SecurityManager, SparkConf}
import org.apache.spark.rpc.HelloEndpoint.SayHello
import org.apache.spark.rpc._

import scala.concurrent.duration.Duration
import scala.concurrent.{Await, Future}


object HelloEndpointRef {
  def main(args: Array[String]): Unit = {
    val conf = new SparkConf()
    val manager = new SecurityManager(conf)
    // 创建client模式的RpcEnv
    val rpcEnv: RpcEnv = RpcEnv.create("hello-server", "localhost", 5432, conf, manager, true)
     // 创建EndpointRef
    val endpointRef: RpcEndpointRef = rpcEnv.setupEndpointRef(RpcAddress("localhost", 5432), "hello-service")
    val future: Future[String] = endpointRef.ask[String](SayHello("spark-rpc"))
    val s = Await.result(future, Duration.apply("30s"))
    print(s)
  }
}
```



## 源码解析 ##

rpc的客户端的具体原理，可以参见此篇博客 {% post_link  spark-rpc-client Spark Rpc 客户端原理 %} 

