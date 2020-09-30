---
title: Kafka Rebalance 客户端原理
date: 2019-03-18 22:11:55
tags: kafka
categories: kafka
---

## 前言

Kafka消费者提供了组的概念，它允许多个 consumer 共同消费一个 topic，而不会造成冲突。Kafka提供了Coordinator服务，负责管理消费组。当有新增的consumer加入组，或者有consumer离开组，都会触发Coordinator的重新平衡操作，Coordinator会将topic的分区重新分配给各个consumer。



## Rebalance 流程

新的consumer加入到组的过程如下：

1. consumer 首先会寻找负责该 consumer grouo 是由哪个节点的 Coordinator 负责
2. 在获取到节点后，consumer 向 Coordinator 发送加入请求
3. Coordinator 会为每个 consumer 分配 id 号，并从中选择出 leader 角色
4. consumer 收到响应后，发现自己被选择为 leader 角色，会执行分区算法，将该topic的分区怎么分配给这个 group 的成员。然后将分配结果发送给Coordinator
5. 如果是follower角色，那么向Coordinator发送请求获取该自己的分区分配结果。

下面我们会按照这个流程，一步步的详细讲解。



## 寻找 Coordinator 地址

consumer第一步是需要找到 Coordinator 的地址，才能进行后续的请求。它从Kafka集群中选择出一个负载最轻的节点，并且发出寻找Coordinator地址的请求。

### 协议格式

请求格式的主要字段：

| 字段名          | 字段类型 | 字段含义 |
| --------------- | -------- | -------- |
| coordinator_key | 字符串   | group id |

响应格式的主要字段：

| 字段名  | 字段类型 | 字段含义                        |
| ------- | -------- | ------------------------------- |
| node_id | 字符串   | coordinator服务所在主机的 id 号 |
| host    | 字符串   | 主机地址                        |
| port    | 整数     | 服务端口号                      |



## 请求加入组

consumer在连接 Coordinator 之后，会与它进行请求交互。它首先会发送加入组的请求，coordinator会分配 id，并且会从组中选出 leader 角色。leader 角色的选取采用先到先得的方式，因为 leader 还会负责分区分配的算法，还需要将结果发送给 Coordinator ，这个过程会比较耗时，所以为了减少整个 rebalance 的时间，所以选用了第一个加入的 consumer。

### 协议格式

请求格式的主要字段：

| 字段名            | 字段类型                | 字段含义                 |
| ----------------- | ----------------------- | ------------------------ |
| group_id          | 字符串                  | consumer 所在的 group id |
| session_timeout   | 整数                    | 心跳超时时间             |
| rebalance_timeout | 整数                    | rebalance超时时间        |
| group_protocols   | group_protocol 类型列表 | group_protocol 类型列表  |

group_protocol 数据格式

| 字段名            | 字段类型 | 字段含义                       |
| ----------------- | -------- | ------------------------------ |
| protocol_name     | 字符串   | consumer支持的分配算法的名称   |
| protocol_metadata | 字节数组 | consumer对于此算法的自定义数据 |



响应格式的主要字段：

| 字段名        | 字段类型        | 字段含义                  |
| ------------- | --------------- | ------------------------- |
| error_code    | 整数            | 错误码                    |
| generation_id | 字符串          | 表示coordinator的数据版本 |
| leader_id     | 整数            | leader角色的 id 号        |
| member_id     | 整数            | 该consumer 的 id 号       |
| members       | member 类型列表 | 所有consumer的订阅信息    |

member 数据格式

| 字段名          | 字段类型 | 字段含义             |
| --------------- | -------- | -------------------- |
| member_id       | 整数     | consumer 的 id 号    |
| member_metadata | 字节数组 | consumer自定义的数据 |



## leader 执行分配

Coordinator 返回给leader角色的响应中，包含了与分配有关的所有信息，比如分区算法和该 group 的所有成员信息。leader角色收到响应后，会执行分区的分配算法，然后将结果保存到 group_assignment 字段里，发送给Coordinator。

### 协议格式

请求格式的主要字段：

| 字段名           | 字段类型            | 字段含义                 |
| ---------------- | ------------------- | ------------------------ |
| group_id         | 字符串              | consumer 所在的 group id |
| generation_id    | 整数                | coordinator的数据版本号  |
| member_id        | 整数                | consumer的 id            |
| group_assignment | assignment 类型列表 | 所有consumer的分配结果   |

assignment 类型格式

| 字段名            | 字段类型 | 字段含义           |
| ----------------- | -------- | ------------------ |
| member_id         | 整数     | consumer的 id      |
| member_assignment | 字节数组 | consumer的分配结果 |



响应格式的主要字段：

| 字段名            | 字段类型 | 字段含义           |
| ----------------- | -------- | ------------------ |
| error_code        | 整数     | 错误码             |
| member_assignment | 字节数组 | consumer的分配结果 |



## follower请求分配结果

follower角色同样发送了`SyncGroupRequest`请求，不过它的groupAssignments字段是空的。Coordinator 会将该consumer的分配结果，返回给它。



## 心跳线程

consumer 会启动一个心跳线程，定时的向Coordinator发送心跳请求，来通知Coordinator自己还活着。

### 协议格式

请求格式的主要字段：

| 字段名        | 字段类型 | 字段含义                 |
| ------------- | -------- | ------------------------ |
| group_id      | 字符串   | consumer 所在的 group id |
| generation_id | 整数     | coordinator的版本号      |
| member_id     | 整数     | consumer的 id            |

响应格式的主要字段：

| 字段名     | 字段类型 | 字段含义 |
| ---------- | -------- | -------- |
| error_code | 整数     | 错误码   |



### 心跳时间

心跳的间隔时间由`heartbeat.interval.ms`配置项指定，默认为3秒。当长时间的没有收到心跳响应，consumer 就会认为超时了，它会认为 Coordinator 已经挂掉了，会将连接断开。这个超时由`session.timeout.ms`配置项指定，默认为10秒。

这里额外提下 poll 超时的问题，kafka 规定两次 poll 的间隔时间必须要小于一定时间，不然会自动的离开 group。这个阈值由`max.poll.interval.ms`配置项指定，默认为5分钟。后面会讲到如何处理这个问题。



## 离开消费组

当 consumer 关闭或者超时等原因，会触发它发起离开消费组的请求。

### 协议格式

请求格式的主要字段：

| 字段名    | 字段类型 | 字段含义                 |
| --------- | -------- | ------------------------ |
| group_id  | 字符串   | consumer 所在的 group id |
| member_id | 整数     | consumer的 id            |

响应格式的主要字段：

| 字段名     | 字段类型 | 字段含义 |
| ---------- | -------- | -------- |
| error_code | 整数     | 错误码   |



## 回调函数

上述介绍完整个 Rebalance 的流程，接下来还需要留意下，kafka 给了一些回调接口，供我们更好的处理 Rebalance 过程。我们只需要实现`ConsumerRebalanceListener`接口，然后调用`KafkaConsumer.subscribe`函数时传递进去即可。

```java
public interface ConsumerRebalanceListener {

    void onPartitionsRevoked(Collection<TopicPartition> partitions);

    void onPartitionsAssigned(Collection<TopicPartition> partitions);

    default void onPartitionsLost(Collection<TopicPartition> partitions) {
        onPartitionsRevoked(partitions);
    }
}
```

kafka 在获取到分区结果后，会调用`onPartitionsAssigned`方法，参数`partitions`表示它所分配的分区结果。

当 consumer 调用 close 方法或者 unsubscribe 方法，会调用`onPartitionsLost`方法，参数`partitions`表示它不在订阅的分区。

当 consumer 触发 balance操作时，会触发`onPartitionsRevoked`方法，参数`partitions`表示那些仅仅需要回收的分区，而不是分配的所有分区。





## 数据版本

我们观察到所有的请求都会携带`generation_id`参数，这是用来表示逻辑时间的。客户端可能因为没来及和服务端沟通，它的信息会落后。当服务端更新消费组的元数据后，`generation_id`就会加一。这样客户端和服务端请求时，服务端就能及时的提醒客户端的数据已经过时了，需要重新获取。





