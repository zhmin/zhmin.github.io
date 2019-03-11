---
title: KafkaProducer 原理
date: 2019-03-11 21:44:15
tags: kafka, producer
categories: kafka
---

# Kafka Producer 原理

Kafka为使用者提供了客户端，负责向Kafka中写入消息，由KafkaProducer实现。KafkaProducer为了提高系统的吞吐量，它会先将消息缓存起来，然后以批次为单位的发送。具体原理如下：

![kafka-producer](kafka-producer.svg)

KafkaProducer将消息发送给RecordAccumulator缓存。

RecordAccumulator会将消息，以ProducerBatch的格式存储起来。它为每一个分区都生成了一个ProducerBatch的队列。

Sender会从RecordAccumulator拉取消息批次ProducerBatch，然后生成请求，通过NetworkClient发送出去。



## 消息生成者 KafkaProducer

KafkaProducer负责将消息序列化，并且确定消息发往哪个节点。它还支持钩子函数，由ProducerInterceptors表示。目前支持发送前的onSend函数，和发送错误后的onSendError函数。

KafkaProducer发送消息有两种方式，一种是不指定callback，一种是指定callback。

```java
public class KafkaProducer<K, V> implements Producer<K, V> {
    // 序列化器
    private final ExtendedSerializer<K> keySerializer;
    private final ExtendedSerializer<V> valueSerializer;
    // 钩子函数集合
    private final ProducerInterceptors<K, V> interceptors;
    // 消息缓冲区
    private final RecordAccumulator accumulator;
    // 默认的分区器
    private final Partitioner partitioner;
    
    
    public Future<RecordMetadata> send(ProducerRecord<K, V> record) {
        return send(record, null);
    }
    public Future<RecordMetadata> send(ProducerRecord<K, V> record, Callback callback) {
        // 调用onSend钩子函数
        ProducerRecord<K, V> interceptedRecord = this.interceptors.onSend(record);
        return doSend(interceptedRecord, callback);
    }
    
    private Future<RecordMetadata> doSend(ProducerRecord<K, V> record, Callback callback) {
        TopicPartition tp = null;
        try {
            // 获取该topic partition的元数据
            ClusterAndWaitTime clusterAndWaitTime = waitOnMetadata(record.topic(), record.partition(), maxBlockTimeMs);
            Cluster cluster = clusterAndWaitTime.cluster;
            // 序列化key值
            byte[] serializedKey;
            try {
                serializedKey = keySerializer.serialize(record.topic(), record.headers(), record.key());
            } catch (ClassCastException cce) {
                throw new SerializationException(...);
            }
            // 序列化value值
            byte[] serializedValue;
            try {
                serializedValue = valueSerializer.serialize(record.topic(), record.headers(), record.value());
            } catch (ClassCastException cce) {
                throw new SerializationException(...);
            }
            // 调用partition函数，确认发往哪个节点
            int partition = partition(record, serializedKey, serializedValue, cluster);
            // 发往消息缓冲区
            RecordAccumulator.RecordAppendResult result = accumulator.append(tp, timestamp, serializedKey, serializedValue, headers, interceptCallback, remainingWaitMs);            
            // 如果此时有batch完成，则调用sender的wakeup方法，通知sender，防止它阻塞
            if (result.batchIsFull || result.newBatchCreated) {
                this.sender.wakeup();
            }
            return result.future;
        } catch (Exception e) {
            // 调用onSendError钩子函数
            this.interceptors.onSendError(record, tp, e);
            throw e;
        }
    }
}
```



上面有个比较重要的partition方法，确定消息发往哪个分区。这个算法由分区器Partitioner表示。kafka有个默认的分区器，同时它也支持自定义。

```java
public class DefaultPartitioner implements Partitioner {
    
    // key为topic名称，value为计数器，初始值为随机分配的。每次添加消息，都会自增
    private final ConcurrentMap<String, AtomicInteger> topicCounterMap = new ConcurrentHashMap<>();
    
    public int partition(String topic, Object key, byte[] keyBytes, Object value, byte[] valueBytes, Cluster cluster) {
        // 获取这个topic的分区数目
        List<PartitionInfo> partitions = cluster.partitionsForTopic(topic);
        int numPartitions = partitions.size();
        if (keyBytes == null) {
            // 如果消息的key值为空，则更新计数器的值
            int nextValue = nextValue(topic);
            // 获取有效的分区（有效是指该分区的leader副本正常运行）
            List<PartitionInfo> availablePartitions = cluster.availablePartitionsForTopic(topic);
            if (availablePartitions.size() > 0) {
                // 取余数
                int part = Utils.toPositive(nextValue) % availablePartitions.size();
                return availablePartitions.get(part).partition();
            } else {
                // 简单取余
                return Utils.toPositive(nextValue) % numPartitions;
            }
        } else {
            // 使用murmur2哈希算法，生成key的哈希值，然后取余 
            return Utils.toPositive(Utils.murmur2(keyBytes)) % numPartitions;
        }
    }
}
```



## 消息缓冲区 RecordAccumulator

RecordAccumulator作为消息缓冲区，它为每个topic partition，生成了一个ProducerBatch的队列。ProducerBatch表示消息批次，每个ProducerBatch也都有大小限制，当它的缓存空间存储满了之后，就会新建一个ProducerBatch。

### ProducerBatch 原理

ProducerBatch包含了多条消息，最终可以转换为MemoryRecords的格式，发送给服务端。

ProducerBatch提供了两个重要的方法：

- tryAppend方法，支持添加消息
- records方法，返回MemoryRecords

ProducerBatch使用MemoryRecordsBuilder类，负责构建MemoryRecords。MemoryRecords是Kafka中保存消息，常见的一种格式。这里需要说明下，Kafka发送消息，是以batch为单位的，每个batch包含了多条消息。MemoryRecords定义了batch的数据格式。

当Kafka发送一个batch后，会得到响应。这个batch响应又包含了里面每个消息的响应。

batch的响应由ProduceRequestResult类表示

```java
public final class ProduceRequestResult {

    private final TopicPartition topicPartition;

    private volatile Long baseOffset = null;
    private volatile long logAppendTime = RecordBatch.NO_TIMESTAMP;
    private volatile RuntimeException error;
}
```

单个消息的响应由FutureRecordMetadata类表示， 它在ProduceRequestResult之上生成

```java
public final class FutureRecordMetadata implements Future<RecordMetadata> {
    private final ProduceRequestResult result;   // batch响应
    private final long relativeOffset;           // 此条消息在batch中的位置
    private final long createTimestamp;          // 创建时间
    private final Long checksum;                 // 校检值
    private final int serializedKeySize;         // key序列化之后的数据长度
    private final int serializedValueSize;       // value序列化之后的数据长度
}
```

ProducerBatch在添加消息的时候，就实例化消息的响应。

```java
public final class ProducerBatch {
    
    // 保存了回调函数，当ProducerBatch发送完成后，会调用里面每条消息的回调函数
    private final List<Thunk> thunks = new ArrayList<>();
    // MemoryRecords构造器
    private final MemoryRecordsBuilder recordsBuilder;
    // 包含消息的数目
    int recordCount;
    // 表示batch响应的future
    final ProduceRequestResult produceFuture;


    public FutureRecordMetadata tryAppend(long timestamp, byte[] key, byte[] value, Header[] headers, Callback callback, long now) {
        // 检查是否有足够的缓存，存储此条消息。
        // 如果没有，则返回null
        if (!recordsBuilder.hasRoomFor(timestamp, key, value, headers)) {
            return null;
        } else {
            // 将消息添加到MemoryRecords构造器里
            Long checksum = this.recordsBuilder.append(timestamp, key, value, headers);
            // 生成此条消息响应数据的future
            // 注意到使用了this.recordCount属性，来表示此条消息在batch中的位置
            FutureRecordMetadata future = new FutureRecordMetadata(this.produceFuture, this.recordCount, timestamp, checksum, key == null ? -1 : key.length, value == null ? -1 : value.length);
            // 将回调函数和future添加到thunks队列
            thunks.add(new Thunk(callback, future));
            // 更新消息数目
            this.recordCount++;
            return future;
        }
    }
    
    public MemoryRecords records() {
        // tryAppend方法会将消息添加到recordsBuilder，这里调用build方法即可生成MemoryRecords
        return recordsBuilder.build();
    }
}
```

在上面添加消息的时候，ProducerBatch将回调函数和响应结果都保存在了thunks列表里。Sender线程在接收完响应后，会调用ProducerBatch的done方法，负责执行回调函数。

```java
public final class ProducerBatch {
    
    public boolean done(long baseOffset, long logAppendTime, RuntimeException exception) {
        // 更新状态
        final FinalState finalState;
        if (exception == null) {
            finalState = FinalState.SUCCEEDED;
        } else {
            finalState = FinalState.FAILED;
        }
        // 执行回调函数
        completeFutureAndFireCallbacks(baseOffset, logAppendTime, exception);
        return true;
    }  
    
    
    // baseOffset为该batch 在 Kafka topic partition 中的位置
    private void completeFutureAndFireCallbacks(long baseOffset, long logAppendTime, RuntimeException exception) {
        // 通过set方法设置batch的响应数据
        produceFuture.set(baseOffset, logAppendTime, exception);

        // 执行每个消息的回调函数
        for (Thunk thunk : thunks) {
            try {
                if (exception == null) {
                    // 获取消息的响应数据，并执行回调函数
                    RecordMetadata metadata = thunk.future.value();
                    if (thunk.callback != null)
                        // 注意这里第二个参数表示异常。如果为null，则表示请求成功
                        thunk.callback.onCompletion(metadata, null);
                } else {
                    // 注意到这儿，如果此次请求异常，只有设置消息的回调函数，才能知道
                    // 第二次参数表示异常实例
                    if (thunk.callback != null)
                        thunk.callback.onCompletion(null, exception);
                }
            } catch (Exception e) {
                log.error("Error executing user-provided callback on message for topic-partition '{}'", topicPartition, e);
            }
        }
        // 表示future已完成
        produceFuture.done();
    }
}
```



### RecordAccumulator 添加消息

RecordAccumulator的append方法，负责添加消息。append方法主要是负责管理ProducerBatch队列，当ProducerBatch数据已满，就会新建ProducerBatch。真正的添加消息是在tryAppend方法里。

```java
public final class RecordAccumulator {
    
    // 消息集合，Key为分区，Value为该分区对应的batch队列
    private final ConcurrentMap<TopicPartition, Deque<ProducerBatch>> batches;
    // ProducerBatch可以保存数据的最小长度
    private final int batchSize;
    // 缓存池
    private final BufferPool free;
    
    public RecordAppendResult append(TopicPartition tp,
                                     long timestamp,
                                     byte[] key,
                                     byte[] value,
                                     Header[] headers,
                                     Callback callback,
                                     long maxTimeToBlock) throws InterruptedException {
        
        ByteBuffer buffer = null;
        if (headers == null) headers = Record.EMPTY_HEADERS;
        try {
            // 获取该分区的batch队列，如果没有就创建
            Deque<ProducerBatch> dq = getOrCreateDeque(tp);
            // 这里使用了锁，保证线程竞争
            synchronized (dq) {
                if (closed)
                    throw new KafkaException("Producer closed while send in progress");
                // 调用tryAppend方法添加，如果返回null，则表示当前ProducerBatch空间已满
                RecordAppendResult appendResult = tryAppend(timestamp, key, value, headers, callback, dq);
                // 表示添加成功
                if (appendResult != null)
                    return appendResult;
            }
            byte maxUsableMagic = apiVersions.maxUsableProduceMagic();
            // 确定batch的大小，比较当前消息的长度和指定最小的长度
            int size = Math.max(this.batchSize, AbstractRecords.estimateSizeInBytesUpperBound(maxUsableMagic, compression, key, value, headers));
            // 从缓冲池中申请内存
            buffer = free.allocate(size, maxTimeToBlock);
            synchronized (dq) {
                if (closed)
                    throw new KafkaException("Producer closed while send in progress");
                // 因为在申请内存的时候，Sender线程有可能刚好提取了消息，所以这里再次尝试调用tryAppend
                // 或者别的线程已经在这个时候，创建完了新的ProducerBatch
                RecordAppendResult appendResult = tryAppend(timestamp, key, value, headers, callback, dq);
                if (appendResult != null) {
                    return appendResult;
                }
                // 生成MemoryRecords构造器，它使用的缓存就是刚刚申请
                MemoryRecordsBuilder recordsBuilder = recordsBuilder(buffer, maxUsableMagic);
                // 新建ProducerBatch
                ProducerBatch batch = new ProducerBatch(tp, recordsBuilder, time.milliseconds());
                // 调用新建的ProducerBatch的tryAppend方法添加数据
                FutureRecordMetadata future = Utils.notNull(batch.tryAppend(timestamp, key, value, headers, callback, time.milliseconds()));
                // 将新建的ProducerBatch，添加到队列里
                dq.addLast(batch);

                // 因为这个buffer已经被新的ProducerBatch所使用，
                // 所以这里将其设置null，防止被释放
                buffer = null;

                return new RecordAppendResult(future, dq.size() > 1 || batch.isFull(), true);
            }
        } finally {
            // 如果申请的buffer没有用到，这里需要将其释放掉
            if (buffer != null)
                free.deallocate(buffer);
        }
    }
    
    // 负责将消息添加到，batch队列中的最后一个
    private RecordAppendResult tryAppend(long timestamp, byte[] key, byte[] value, Header[] headers, Callback callback, Deque<ProducerBatch> deque) {
        // 取出最后的ProducerBatch
        ProducerBatch last = deque.peekLast();
        if (last != null) {
            // 调用batch的tryAppend添加
            FutureRecordMetadata future = last.tryAppend(timestamp, key, value, headers, callback, time.milliseconds());
            // 如果返回null，表示当前batch的空间已满
            if (future == null)
                // 关闭当前batch的添加
                last.closeForRecordAppends();
            else
                // 返回RecordAppendResult
                return new RecordAppendResult(future, deque.size() > 1 || last.isFull(), false);
        }
        return null;
    }
}
```



### RecordAccumulator 消费消息

Sender作为消息的消费者，它涉及到RecordAccumulator的两个方法：

1. RecordAccumulator的ready方法负责，来查看哪些节点的请求需要发送。

1. RecordAccumulator的drain方法负责，从队列中提取消息。目前这里先不考虑事务，代码简化如下

首先看ready方法，它会根据好几个方面，来判断是否应该发送请求。只要满足下面一种，就会认为需要发送

- 内存紧张时，因为RecordAccumulator会一直保存消息，占用内存，一直到消息发送完成才会释放
- 消息堆积一直没有发送，堆积时间超过了指定值
- 消息重试的时间间隔，已经过去
- 该batch存储消息的空间已满，需要立即发送

```java
public final class RecordAccumulator {
    
    // 消息集合，Key为分区，Value为该分区对应的batch队列
    private final ConcurrentMap<TopicPartition, Deque<ProducerBatch>> batches;    
    
    public ReadyCheckResult ready(Cluster cluster, long nowMs) {
        Set<Node> readyNodes = new HashSet<>();
        long nextReadyCheckDelayMs = Long.MAX_VALUE;
        Set<String> unknownLeaderTopics = new HashSet<>();

        // 查看缓冲池是否资源紧张
        boolean exhausted = this.free.queued() > 0;
        // 遍历batch
        for (Map.Entry<TopicPartition, Deque<ProducerBatch>> entry : this.batches.entrySet()) {
            // 获取topic partition 和 消息队列
            TopicPartition part = entry.getKey();
            Deque<ProducerBatch> deque = entry.getValue();
            
            // 查看topic partition的 leader副本所在的节点，因为只有leader副本才能处理写请求
            Node leader = cluster.leaderFor(part);
            synchronized (deque) {
                if (leader == null && !deque.isEmpty()) {
                    unknownLeaderTopics.add(part.topic());
                } else if (!readyNodes.contains(leader) && !muted.contains(part)) {
                    ProducerBatch batch = deque.peekFirst();
                    if (batch != null) { 
                        long waitedTimeMs = batch.waitedTimeMs(nowMs);
                        // 检查是否在重试时间内
                        boolean backingOff = batch.attempts() > 0 && waitedTimeMs < retryBackoffMs;
                        // 计算batch停留的最大时间， 
                        // 当batch的空间一直没有写满，它允许停留的最大时间不能超过 lingerMs
                        // 当batch在重试期间内，重试间隔必须大于 retryBackoffMs
                        long timeToWaitMs = backingOff ? retryBackoffMs : lingerMs;
                        // 该topic partition是有有已经完成的batch，
                        // 如果batch的数目大于1，说明第一个batch肯定已经完成了，才会新建第二个batch
                        // 如果只有一个batch，那么查看这个batch是否已经完成
                        boolean full = deque.size() > 1 || batch.isFull();
                        // batch停留的时间超过最大时间
                        boolean expired = waitedTimeMs >= timeToWaitMs;
                        // 满足上述条件的一种，则表示允许发送
                        boolean sendable = full || expired || exhausted || closed || flushInProgress();
                        if (sendable && !backingOff) {
                            // 添加到readyNodes列表
                            readyNodes.add(leader);
                        } else {
                            long timeLeftMs = Math.max(timeToWaitMs - waitedTimeMs, 0);
                            nextReadyCheckDelayMs = Math.min(timeLeftMs, nextReadyCheckDelayMs);
                        }
                    }
                }
            }
        }
        return new ReadyCheckResult(readyNodes, nextReadyCheckDelayMs, unknownLeaderTopics);
    }
}
```



接下来看drain方法，如何提取消息的。它会找到节点的所有topic partition，然后从对应的ProducerBatch队列中提取。

```java
// nodes参数，表示限制请求是这些节点
public Map<Integer, List<ProducerBatch>> drain(Cluster cluster, Set<Node> nodes, int maxSize, long now) {
    Map<Integer, List<ProducerBatch>> batches = new HashMap<>();
    for (Node node : nodes) {
        int size = 0;
        // 获取该节点有哪些分区
        List<PartitionInfo> parts = cluster.partitionsForNode(node.id());
        List<ProducerBatch> ready = new ArrayList<>();
        // drainIndex是RecordAccumulator类的一个属性，
        // 通过它可以按照顺序遍历，而不必每次都是从第一个位置开始
        int start = drainIndex = drainIndex % parts.size();
        do {
            PartitionInfo part = parts.get(drainIndex);
            TopicPartition tp = new TopicPartition(part.topic(), part.partition());
            // 当启动消息发送的幂等性，需要要求batch发送完成后，才能继续发送新的batch
            if (!isMuted(tp, now)) {
                // 获取topic partition的消息队列
                Deque<ProducerBatch> deque = getDeque(tp);
                if (deque != null) {
                    synchronized (deque) {
                        ProducerBatch first = deque.peekFirst();
                        if (first != null) {
                            // 检查是否，消息发送失败正在重试，因为重试有时间间隔要求的
                            boolean backoff = first.attempts() > 0 && first.waitedTimeMs(now) < retryBackoffMs;
                            if (!backoff) {
                                // 检查batch是否超过了指定的最大值
                                if (size + first.estimatedSizeInBytes() > maxSize && !ready.isEmpty()) {
                                    break;
                                } else {
                                    // 取出batch，添加到ready列表里
                                    ProducerIdAndEpoch producerIdAndEpoch = null;
                                    boolean isTransactional = false;
                                    ProducerBatch batch = deque.pollFirst();
                                    batch.close();
                                    size += batch.records().sizeInBytes();
                                    ready.add(batch);
                                    // 设置batch的drain的时间点
                                    batch.drained(now);
                                }
                            }
                        }
                    }
                }
            }
            // 更新drainIndex，指向下一个
            this.drainIndex = (this.drainIndex + 1) % parts.size();
        } while (start != drainIndex);
        // 将这个节点的请求，添加到batches集合
        batches.put(node.id(), ready);
    }
    return batches;
}
```



## 消息发送者 Sender

Sender实现了Runnable接口，它运行在一个单独的线程里。它会循环的从RecordAccumulator获取消息，并且通过NetworkClient发送消息。

```java
public class Sender implements Runnable {
    private final KafkaClient client;
    private final RecordAccumulator accumulator;
    private final Metadata metadata;
    
    public void run() {
        while (running) {
            run(time.milliseconds());
        }
        ......
    }
    
    void run(long now) {
        if (transactionManager != null) {
            ...... // 这里暂时不讨论事务
        }
        // 调用sendProducerData发送消息
        long pollTimeout = sendProducerData(now);
        // 调用client的poll方法发送消息和处理响应
        client.poll(pollTimeout, now);
    }
    private long sendProducerData(long now) {
        // 获取元数据
        Cluster cluster = metadata.fetch();
        // 从accumulator获取，需要发送消息给哪些节点
        RecordAccumulator.ReadyCheckResult result = this.accumulator.ready(cluster, now);
        // 如果存在leader未知的情况，请求更新元数据
        if (!result.unknownLeaderTopics.isEmpty()) { 
            for (String topic : result.unknownLeaderTopics)
                this.metadata.add(topic);
            this.metadata.requestUpdate();
        }
        // 从accumulator获取消息
        Map<Integer, List<ProducerBatch>> batches = this.accumulator.drain(cluster, result.readyNodes, this.maxRequestSize, now);
        // 当请求数目过多，或者网络原因，导致有些batch很久都未能发送出去
        // 这里会认为batch失败，不再重新发送
        List<ProducerBatch> expiredBatches = this.accumulator.expiredBatches(this.requestTimeout, now);
        for (ProducerBatch expiredBatch : expiredBatches) {
            // 调用failBatch处理过期的batch
            failBatch(expiredBatch, -1, NO_TIMESTAMP, expiredBatch.timeoutException(), false);
        }
        // 调用sendProduceRequests发送请求
        sendProduceRequests(batches, now);
    }               
}
```

sendProduceRequests方法会为每个节点，构造请求，并且调用NetworkClient发送出去。

```java
private void sendProduceRequest(long now, int destination, short acks, int timeout, List<ProducerBatch> batches) {
    // 存储着要发送的消息，会被用在构建ProduceRequest请求
    Map<TopicPartition, MemoryRecords> produceRecordsByPartition = new HashMap<>(batches.size());
    // 保存着发送的batch，会被用在回调函数里，处理响应
    final Map<TopicPartition, ProducerBatch> recordsByPartition = new HashMap<>(batches.size());
    // 遍历batch
    for (ProducerBatch batch : batches) {
        TopicPartition tp = batch.topicPartition;
        // 生成 MemoryRecords
        MemoryRecords records = batch.records();
        // 将records保存到produceRecordsByPartition集合
        produceRecordsByPartition.put(tp, records);
        // 将batch保存在recordsByPartition集合里
        recordsByPartition.put(tp, batch);
    }
        
    // 实例ProduceRequest请求构造器
    ProduceRequest.Builder requestBuilder = ProduceRequest.Builder.forMagic(minUsedMagic, acks, timeout, produceRecordsByPartition, transactionalId);
    // 生成回调函数，本质调用了handleProduceResponse方法
    RequestCompletionHandler callback = new RequestCompletionHandler() {
        public void onComplete(ClientResponse response) {
            handleProduceResponse(response, recordsByPartition, time.milliseconds());
        }
    };
    String nodeId = Integer.toString(destination);
    // 生成请求
    ClientRequest clientRequest = client.newClientRequest(nodeId, requestBuilder, now, acks != 0, callback);
    // 调用NetworkClient发送请求
    client.send(clientRequest, now);
}
```



注意到上面的回调函数，它会处理响应。它会解析请求，然后执行每个batch的回调函数。而每个batch会为每个它的每条消息，生成响应，并且执行每条消息的回调。

```java
private void handleProduceResponse(ClientResponse response, Map<TopicPartition, ProducerBatch> batches, long now) {
    // 解析响应，获取ProduceResponse
    ProduceResponse produceResponse = (ProduceResponse) response.responseBody();
    for (Map.Entry<TopicPartition, ProduceResponse.PartitionResponse> entry : produceResponse.responses().entrySet()) {
        TopicPartition tp = entry.getKey();
        ProduceResponse.PartitionResponse partResp = entry.getValue();
        ProducerBatch batch = batches.get(tp);
        // completeBatch负责执行回调，最终是调用了completeBatch方法
        completeBatch(batch, partResp, correlationId, now);
    }    
}

private void completeBatch(ProducerBatch batch, ProduceResponse.PartitionResponse response) {
    // 调用batch的done方法，触发batch回调
    if (batch.done(response.baseOffset, response.logAppendTime, null))
        this.accumulator.deallocate(batch);
}
```