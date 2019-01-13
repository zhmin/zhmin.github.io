---
title: spark-rpc-client
date: 2019-01-10 23:58:30
tags:
---

## Spark Rpc 客户端 ##



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





## rpc服务端处理请求 ##

<img src="rpc-server-receive.svg">

TrasnportServer： Netty服务端，负责接收消息

EventLoopGroup : Netty客户端的工作线程池，当收到client的请求时，会处理请求

Dispatcher : 分发器，将请求分发给对应的Inbox

Inbox ： 收件箱，每个RpcEndpoint都有独立的收件箱，存储着请求

RpcEndpoint ： Rpc服务端

