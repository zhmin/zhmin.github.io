---
title: Spark ExternalShuffleService 运行原理
date: 2019-08-05 22:18:00
tags: spark, external-shuffle-service
categories: spark
---

## 前言

Spark 的 Executor 节点不仅负责数据的计算，还涉及到数据的管理。如果发生了 shuffle 操作，Executor 节点不仅需要生成 shuffle 数据，还需要负责处理读取请求。如果 一个 Executor 节点挂掉了，那么它也就无法处理 shuffle 的数据读取请求了，它之前生成的数据都没有意义了。

为了解耦数据计算和数据读取服务，Spark 支持单独的服务来处理读取请求。这个单独的服务叫做 ExternalShuffleService，运行在每台主机上，管理该主机的所有 Executor 节点生成的 shuffle 数据。有读者可能会想到性能问题，因为之前是由多个 Executor 负责处理读取请求，而现在一台主机只有一个 ExternalShuffleService 处理请求，其实性能问题不必担心，因为它主要消耗磁盘和网络，而且采用的是异步读取，所以并不会有性能影响。

解耦之后，如果 Executor 在数据计算时不小心挂掉，也不会影响 shuffle 数据的读取。而且Spark 还可以实现动态分配，动态分配是指空闲的 Executor 可以及时释放掉。



## 架构图

下图展示了一个 shuffle 操作，有两台主机分别运行了三个 Map 节点。Map 节点生成完 shuffle 数据后，会将数据的文件路径告诉给 ExternalShuffleService。之后的 Reduce 节点读取数据，就只和 ExternalShuffleService 交互。

<img src="spark-external-shuffle-service.svg">



## Yarn AuxServices

本篇主要是讲 Spark 运行在 Yarn 的场景，ExternalShuffleService 是作为 Yan 的辅助服务运行的，这里先讲讲 Yarn 的辅助服务。

Yarn NodeManager 服务启动的时候，会启动一些额外的辅助服务，这些辅助服务的运行周期同 NodeManager 相同。每个辅助服务由 AuxiliaryService 类表示，用户需要继承并实现 AuxiliaryService 类，并且还需要配置 Yarn 的 yarn.nodemanager.aux-services选项，这样才能被 NodeManager 发现。下面添加了 spark_shuffle 名称的服务，并且指定了实现的类名和端口号。

```xml
<property>
    <name>yarn.nodemanager.aux-services</name>
    <value>mapreduce_shuffle, spark_shuffle</value>
</property>

<property>
    <name>yarn.nodemanager.aux-services.spark_shuffle.class</name>
    <value>org.apache.spark.network.yarn.YarnShuffleService</value>
</property>

<property>
    <name>spark.shuffle.service.port</name>
    <value>7337</value>
</property>
```



## YarnShuffleService

从上面的配置中，可以看到实现类 是 YarnShuffleService。它本质是运行了一个 Spark Rpc 服务，只处理来自 Map 节点和来自 Reduce 节点的请求。 

1. Map 节点会将生成好的 shuffle 文件路径通知给 ExternalShuffleService ，请求由 RegisterExecutor 表示。
2. Reduce 节点读取数据的流程，先发送 OpenBlocks 请求获取 streamId，然后通过 streamId 来请求数据。

解析来依次来看看这些请求的原理。



## RegisterExecutor 请求

首先看看 RegisterExecutor 请求的格式：

```java
public class RegisterExecutor extends BlockTransferMessage {
    public final String appId;         // spark application id
    public final String execId;        // executor id
    public final ExecutorShuffleInfo executorInfo;    // 文件路径
}


```

因为 ExternalShuffleService 是为同个主机上所有 Executor 服务的，而这台主机上可能同时运行多个 spark 程序，所以需要 appId 用来区分。execId 是用来区分 Executor 节点的。executorInfo 是用来指定数据存储的目录。

继续看看文件路径是怎么定义的

```java
public class ExecutorShuffleInfo implements Encodable {
    
    public final String[] localDirs;         // 第一级目录列表
    public final int subDirsPerLocalDir;     // 第二级目录列表
    public final String shuffleManager;      // shuffleManager的类型，目前只有一种类型 SortShuffleManager
}
```

上面只是定义了两级目录，如果想要了解它的含义，就必须了解 shuffle 数据存储位置的规则。

Executor 在 存储 shuffle 数据时，是将它存放在分散的目录，分散的目录是根据文件名的哈希值来确定的。我们知道 shuffle 的数据文件名是有着固定的格式的，`shuffle_{shuffleId}_{mapId}_{reduceId}.data`，因为目前 shuffle 实现的时候，为了减少文件数过多造成的性能差，会将多份数据合并成一个大文件，对应的最终文件名 `shuffle_{shuffleId}_{mapId}_0.data`,并且为了快速查找 reduceId 对应的数据位置，还生成了一份索引文件 `shuffle_{shuffleId}_{mapId}_0.index`。

YarnShuffleService 服务会将这些位置信息存储到内存中，如果指定了持久化，会保存到 leveldb 中。



## 客户端读取

客户端会根据数据的所在节点，流程同普通的一样，只不过普通是向 Executor 进程发送请求，而这里是向 YarnShuffleService 发出读取请求。它分为两部请求：OpenBlocks 请求 和数据传输请求。

### OpenBlocks 请求

```java
public class OpenBlocks extends BlockTransferMessage {
    public final String appId;    // spark application id
    public final String execId;   // executor id
    public final String[] blockIds;    // 包含shuffleId，mapId，reduceId 的字符串列表
}
```

YarnShuffleService 会根据请求的数据，找到对应的文件路径，并且根据 reduceId 从索引文件中，找到数据所在的文件偏移值。



### 数据传输请求

客户端根据第一步返回的streamId 和 chunkId 列表，向 YarnShuffleService 发送数据传输请求。

