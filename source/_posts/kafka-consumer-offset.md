---
title: Kafka Consumer 管理 Offset 原理
date: 2019-04-08 23:02:09
tags: kafka, consumer, offset
categories: kafka
---

# Kafka Consumer 管理 Offset 原理



## Consumer 元数据

Consumer本地也保存了关于管理Offset的元数据，它分为两部分，订阅信息和分区信息。consumer拉取消息之前，首先要确认分配到了哪些分区，这些数据就属于订阅相关的信息。之后consumer拉取消息后，会更新客户端本地的offset，这些数据就属于分区相关的信息。

### 订阅信息

Consumer有两种消费模式，订阅模式和分配模式。订阅模式是consumer只需要指定哪些topic需要订阅，由GroupCoordinator分配分区。分配模式是consumer自己指定哪些分区。这些信息都是保存在SubscriptionState类里

```java
public class SubscriptionState {
    // 订阅类型
    // AUTO_TOPICS 属于订阅模式，它指定哪些topic
    // AUTO_PATTERN 属于订阅模式，它使用正则匹配topic
    // USER_ASSIGNED 属于分配模式
    private enum SubscriptionType {
        NONE, AUTO_TOPICS, AUTO_PATTERN, USER_ASSIGNED
    }
    
    // 采用哪种订阅模式
    private SubscriptionType subscriptionType;

    // 订阅的正则表达式，只有订阅类型为AUTO_PATTERN，才会被使用
    private Pattern subscribedPattern;

    // 订阅的topic名称列表，只有订阅类型为AUTO_TOPICS，才会被使用
    private Set<String> subscription;
}
```



当consumer使用订阅模式，收到服务端返回的分区分配结果后，会保存起来。我们回想一下ConsumerCoordinator的原理，当它收到分配结果后，会调用onJoinComplete回调函数，这里会处理分配结果。

```java
public final class ConsumerCoordinator extends AbstractCoordinator {
  
    private final SubscriptionState subscriptions;    
    
    @Override
    protected void onJoinComplete(int generation,
                                  String memberId,   // 该成员 id
                                  String assignmentStrategy,   // 分配算法
                                  ByteBuffer assignmentBuffer) {   // 序列化之后的分配结果
        // 解析分配结果
        Assignment assignment = ConsumerProtocol.deserializeAssignment(assignmentBuffer);
        // 保存到SubscriptionState类
        subscriptions.assignFromSubscribed(assignment.partitions());                                       ......
    }
}                                 
```

SubscriptionState提供了assignFromSubscribed方法，来保存订阅模式的分配结果

```java
public class SubscriptionState {
    // 分配的分区信息
    private final PartitionStates<TopicPartitionState> assignment;

    public void assignFromSubscribed(Collection<TopicPartition> assignments) {
        // 为这些分区，初始化分区信息 TopicPartitionState
        Map<TopicPartition, TopicPartitionState> assignedPartitionStates = partitionToStateMap(assignments);
        // 触发回调函数
        fireOnAssignment(assignedPartitionStates.keySet());
        // 保存这些分区的信息
        this.assignment.set(assignedPartitionStates);
    }
}
```



当consumer使用分配模式时，必须手动指定分区。KafkaConsumer类提供了assign方法来指定分区

```java
public class KafkaConsumer<K, V> implements Consumer<K, V> {

    @Override
    public void assign(Collection<TopicPartition> partitions) {
        acquireAndEnsureOpen();
        try {
            // 检查指定的分区不为 null
            if (partitions == null) {
                throw new IllegalArgumentException("Topic partition collection to assign to cannot be null");
            } else if (partitions.isEmpty()) {
                //如果 partitions 为空，那么表明是不在消费消息
                this.unsubscribe();
            } else {
                // 指定的分区涉及到的topic列表
                Set<String> topics = new HashSet<>();
                for (TopicPartition tp : partitions) {
                    String topic = (tp != null) ? tp.topic() : null;
                    if (topic == null || topic.trim().isEmpty())
                        throw new IllegalArgumentException("Topic partitions to assign to cannot have null or empty topic");
                    topics.add(topic);
                }

                // make sure the offsets of topic partitions the consumer is unsubscribing from
                // are committed since there will be no following rebalance
                this.coordinator.maybeAutoCommitOffsetsAsync(time.milliseconds());

                // 将分区保存到SubscriptionState里
                this.subscriptions.assignFromUser(new HashSet<>(partitions));
                // 更新Metadata获取哪些topic的元数据
                metadata.setTopics(topics);
            }
        } finally {
            release();
        }
    }
}
```



SubscriptionState提供了assignFromSubscribed方法，来保存订阅模式的分配结果

```java
public class SubscriptionState {
  
    private final PartitionStates<TopicPartitionState> assignment;
    
    public void assignFromUser(Set<TopicPartition> partitions) {
        // 设置订阅类型为 USER_ASSIGNED
        setSubscriptionType(SubscriptionType.USER_ASSIGNED);

        if (!this.assignment.partitionSet().equals(partitions)) {
            // 执行回调函数
            fireOnAssignment(partitions);
            // 为每个分区，初始化分区信息
            Map<TopicPartition, TopicPartitionState> partitionToState = new HashMap<>();
            for (TopicPartition partition : partitions) {
                TopicPartitionState state = assignment.stateValue(partition);
                if (state == null)
                    state = new TopicPartitionState();
                partitionToState.put(partition, state);
            }
            // 保存到assignment
            this.assignment.set(partitionToState);
        }
    }
}
```



### 分区信息

consumer为分配的分区，保存了对应的信息，以TopicPartitionState类表示。这些分区的消息都会保存在SubscriptionState类里。

```java
private static class TopicPartitionState {
    private Long position; // 消费位置
    private Long highWatermark; // 高水位
    private Long logStartOffset; // the log start offset
    private Long lastStableOffset;
    private boolean paused;  // whether this partition has been paused by the user
    private OffsetResetStrategy resetStrategy;  // 如何初始化consumer的消费位置
    private Long nextAllowedRetryTimeMs;
}
```

 

## 获取 Offset 初始值

KafkaConsumer每次发送请求时，都需要先指定开始位置。如果该consumer所属的组，有消费记录，那么就会上次消费位置开始。如果没有，则需要根据auto.offset.reset配置项，来判断从分区的开始位置，还是分区的最新位置读取。

KafkaConsumer 的 poll 方法负责拉取数据。poll 方法每次都会先检查分区的初始消费位置，负责检查消费位置由updateAssignmentMetadataIfNeeded方法实现。

```java
public class KafkaConsumer<K, V> implements Consumer<K, V> {
    
    private final ConsumerCoordinator coordinator;    
    private final SubscriptionState subscriptions;
        
    boolean updateAssignmentMetadataIfNeeded(final long timeoutMs) {
        final long startMs = time.milliseconds();
        // 调用coordinator的poll方法，处理组相关的事件
        // 返回ture表示已经成功加入组并且获得分配结果了
        if (!coordinator.poll(timeoutMs)) {
            return false;
        }
        // 调用updateFetchPositions，来获取消费位置
        return updateFetchPositions(remainingTimeAtLeastZero(timeoutMs, time.milliseconds() - startMs));
    }
    
    private boolean updateFetchPositions(final long timeoutMs) {
        // 查看SubscriptionState是否已经获取了消费位置，如果有则直接返回
        cachedSubscriptionHashAllFetchPositions = subscriptions.hasAllFetchPositions();
        if (cachedSubscriptionHashAllFetchPositions) return true;

        // 这里通过coordinator获取，该consumer组的消费位置
        if (!coordinator.refreshCommittedOffsetsIfNeeded(timeoutMs)) return false;

        // 如果consumer组没有消费位置，那么需要获取它的位置初始化策略
        subscriptions.resetMissingPositions();

        // 获取分区的开始位置或者末尾位置
        fetcher.resetOffsetsIfNeeded();

        return true;
    }
    
}    
```



### coordinator 获取消费位置

KafkaConsumer 会优先查看该组，是否有消费记录。coordinator的refreshCommittedOffsetsIfNeeded方法，负责向GroupCoordinator请求上次消费位置，并且结果保存到SubscriptionState里。

```java
public final class ConsumerCoordinator extends AbstractCoordinator {
    
    private final SubscriptionState subscriptions;
    
    // 正在发送的获取消费位置的请求
    private PendingCommittedOffsetRequest pendingCommittedOffsetRequest = null;

    public boolean refreshCommittedOffsetsIfNeeded(final long timeoutMs) {
        // 查看有哪些分区，它的消费位置还没初始化
        final Set<TopicPartition> missingFetchPositions = subscriptions.missingFetchPositions();
        // 发送请求，获取消费位置
        final Map<TopicPartition, OffsetAndMetadata> offsets = fetchCommittedOffsets(missingFetchPositions, timeoutMs);
        if (offsets == null) return false;
        // 遍历响应
        for (final Map.Entry<TopicPartition, OffsetAndMetadata> entry : offsets.entrySet()) {
            final TopicPartition tp = entry.getKey();
            final long offset = entry.getValue().offset();
            // 将消费位置设置保存到SubscriptionState
            this.subscriptions.seek(tp, offset);
        }
        return true;
    }

    // 向GroupCoordinator发送请求
    public Map<TopicPartition, OffsetAndMetadata> fetchCommittedOffsets(final Set<TopicPartition> partitions, final long timeoutMs) {
        if (partitions.isEmpty()) return Collections.emptyMap();

        final Generation generation = generation();
        if (pendingCommittedOffsetRequest != null && !pendingCommittedOffsetRequest.sameRequest(partitions, generation)) {
            // 如果之前的消费位置请求，和此次请求的内容不一样，
            // 那么就重新请求，并且删除掉旧有请求
            pendingCommittedOffsetRequest = null;
        }

        final long startMs = time.milliseconds();
        long elapsedTime = 0L;

        while (true) {
            // 保证与GroupCoordinator的连接
            if (!ensureCoordinatorReady(remainingTimeAtLeastZero(timeoutMs, elapsedTime))) return null;
            elapsedTime = time.milliseconds() - startMs;
            
            final RequestFuture<Map<TopicPartition, OffsetAndMetadata>> future;
            if (pendingCommittedOffsetRequest != null) {
                // 如果有正在发送的请求，那么就不再发送请求
                future = pendingCommittedOffsetRequest.response;
            } else {
                // 如果没有正在发送的请求，那么就需要发送请求
                future = sendOffsetFetchRequest(partitions);
                pendingCommittedOffsetRequest = new PendingCommittedOffsetRequest(partitions, generation, future);

            }
            // 等待请求完成
            client.poll(future, remainingTimeAtLeastZero(timeoutMs, elapsedTime));

            // 如果请求完成，则返回响应
            // 如果未完成，则返回null
            if (future.isDone()) {
                pendingCommittedOffsetRequest = null;

                if (future.succeeded()) {
                    return future.value();
                } else if (!future.isRetriable()) {
                    throw future.exception();
                } else {
                    elapsedTime = time.milliseconds() - startMs;
                    final long sleepTime = Math.min(retryBackoffMs, remainingTimeAtLeastZero(startMs, elapsedTime));
                    time.sleep(sleepTime);
                    elapsedTime += sleepTime;
                }
            } else {
                return null;
            }
        }
    }
}
```



消费位置的请求格式由OffsetFetchRequest表示，它主要包含以下字段

```java
public class OffsetFetchRequest extends AbstractRequest {
    private final String groupId;  // consumer组的名称
    private final List<TopicPartition> partitions;  // 请求的分区列表
}
```

消费位置的响应格式由OffsetFetchResponse表示，它主要包含以下字段

```java
public class OffsetFetchResponse extends AbstractResponse {
    
    private final Errors error; // 是否响应出错
    
    private final Map<TopicPartition, PartitionData> responseData; // 分区的消费信息
    
    public static final class PartitionData {
        public final long offset; // 消费位置
        public final String metadata;  // consumer的自定义数据
        public final Errors error;  // 该分区的响应是否出错
    }
}
```



### 消费位置初始化

消费位置初始化策略，由枚举OffsetResetStrategy表示。 它有下面三种

```java
public enum OffsetResetStrategy {
    LATEST, EARLIEST, NONE
}
```

LATEST 策略 ： 选择此时分区的末尾位置。因为有可能会有新的数据添加进来，这样末尾位置就会改变，所以只选择此时的位置。

EARLIEST 策略：选择此时目前分区的开始位置。因为有可能会有旧的数据被删除，这样开始位置就会改变，所以只选择此时的位置。

NONE 策略：表示没有指定

KafkaConsumer会在初始化时，指定策略。

```java
public class KafkaConsumer<K, V> implements Consumer<K, V> {
    private KafkaConsumer(ConsumerConfig config, Deserializer<K> keyDeserializer, Deserializer<V> valueDeserializer) {
        ......
        // 根据配置选项，实例化策略
        OffsetResetStrategy offsetResetStrategy = OffsetResetStrategy.valueOf(config.getString(ConsumerConfig.AUTO_OFFSET_RESET_CONFIG).toUpperCase(Locale.ROOT));
        // 实例化SubscriptionState时，保存策略保存
        this.subscriptions = new SubscriptionState(offsetResetStrategy);
        .....   
    }
```

SubscriptionState将策略保存到 defaultResetStrategy 属性，并且它提供了resetMissingPositions方法，为每个分区设置默认策略。

```java
public class SubscriptionState {

    private final OffsetResetStrategy defaultResetStrategy;
    
    public SubscriptionState(OffsetResetStrategy defaultResetStrategy) {
        this.defaultResetStrategy = defaultResetStrategy;
        ......
    }

    public void resetMissingPositions() {
        final Set<TopicPartition> partitionsWithNoOffsets = new HashSet<>();
        // 遍历分区信息列表
        for (PartitionStates.PartitionState<TopicPartitionState> state : assignment.partitionStates()) {
            TopicPartition tp = state.topicPartition();
            TopicPartitionState partitionState = state.value();
            
            if (partitionState.isMissingPosition()) {
                // 如果消费位置没有指定，并且还没有默认策略，那么就会报错
                if (defaultResetStrategy == OffsetResetStrategy.NONE)
                    partitionsWithNoOffsets.add(tp);
                else
                    // 设置分区的策略
                    partitionState.reset(defaultResetStrategy);
            }
        }

        if (!partitionsWithNoOffsets.isEmpty())
            throw new NoOffsetForPartitionException(partitionsWithNoOffsets);
    }
}
```



当分区都设置好了策略，Fetcher会根据策略，发送请求获取消费的初始位置。请求格式如下：

| 字段       | 类型                           | 注释                |
| ---------- | ------------------------------ | ------------------- |
| replica_id | String                         | 服务地址            |
| topics     | LIST_OFFSET_REQUEST_TOPIC 列表 | 请求topic的信息列表 |

LIST_OFFSET_REQUEST_TOPIC 格式

| 字段       | 类型                               | 注释         |
| ---------- | ---------------------------------- | ------------ |
| topic      | String                             | topic名称    |
| partitions | LIST_OFFSET_REQUEST_PARTITION 列表 | 分区信息列表 |

LIST_OFFSET_REQUEST_PARTITION 格式

| 字段      | 类型   | 注释             |
| --------- | ------ | ---------------- |
| partition | String | 分区 id          |
| timestamp | String | 查找记录的时间点 |

注意到这里的 timestamp 字段，比较特殊。当timestamp的值为 -2，那么表示请求该分区的开始位置。如果为 -1，表示请求该分区的末尾位置。



Fetcher会根据策略，设置好timestamp字段，然后发送请求。当收到响应后，会把结果存储到SubscriptionState。

```java
public class Fetcher<K, V> implements SubscriptionState.Listener, Closeable {

    private final AtomicReference<RuntimeException> cachedListOffsetsException = new AtomicReference<>();
    
    private final SubscriptionState subscriptions;
    
    public void resetOffsetsIfNeeded() {
        // Raise exception from previous offset fetch if there is one
        RuntimeException exception = cachedListOffsetsException.getAndSet(null);
        if (exception != null)
            throw exception;

        // 寻找那些需要初始化位置的分区
        Set<TopicPartition> partitions = subscriptions.partitionsNeedingReset(time.milliseconds());
        if (partitions.isEmpty())
            return;

        final Map<TopicPartition, Long> offsetResetTimestamps = new HashMap<>();
        for (final TopicPartition partition : partitions) {
            // 根据策略类型，获取timestamp字段的值
            Long timestamp = offsetResetStrategyTimestamp(partition);
            if (timestamp != null)
                offsetResetTimestamps.put(partition, timestamp);
        }
        // 发送请求，并且处理响应
        resetOffsetsAsync(offsetResetTimestamps);
    }
    
    private Long offsetResetStrategyTimestamp(final TopicPartition partition) {
        // 获取分区的初始化策略
        OffsetResetStrategy strategy = subscriptions.resetStrategy(partition);
        if (strategy == OffsetResetStrategy.EARLIEST)
            // 如果是EARLIEST策略，则返回 -1
            return ListOffsetRequest.EARLIEST_TIMESTAMP;
        else if (strategy == OffsetResetStrategy.LATEST)
            // 如果是LATEST策略，则返回 -2
            return ListOffsetRequest.LATEST_TIMESTAMP;
        else
            return null;
    }   
    
    
    private void resetOffsetsAsync(Map<TopicPartition, Long> partitionResetTimestamps) {
        // Add the topics to the metadata to do a single metadata fetch.
        for (TopicPartition tp : partitionResetTimestamps.keySet())
            metadata.add(tp.topic());

        // 将这些分区的请求，按照节点进行划分
        Map<Node, Map<TopicPartition, Long>> timestampsToSearchByNode = groupListOffsetRequests(partitionResetTimestamps);
        
        for (Map.Entry<Node, Map<TopicPartition, Long>> entry : timestampsToSearchByNode.entrySet()) {
            Node node = entry.getKey();
            final Map<TopicPartition, Long> resetTimestamps = entry.getValue();
            subscriptions.setResetPending(resetTimestamps.keySet(), time.milliseconds() + requestTimeoutMs);
            // 发送请求
            RequestFuture<ListOffsetResult> future = sendListOffsetRequest(node, resetTimestamps, false);
            // 添加回调函数
            future.addListener(new RequestFutureListener<ListOffsetResult>() {
                @Override
                public void onSuccess(ListOffsetResult result) {
                    if (!result.partitionsToRetry.isEmpty()) {
                        subscriptions.resetFailed(result.partitionsToRetry, time.milliseconds() + retryBackoffMs);
                        metadata.requestUpdate();
                    }
                    // 遍历分区结果
                    for (Map.Entry<TopicPartition, OffsetData> fetchedOffset : result.fetchedOffsets.entrySet()) {
                        TopicPartition partition = fetchedOffset.getKey();
                        // 获取位置
                        OffsetData offsetData = fetchedOffset.getValue();
                        Long requestedResetTimestamp = resetTimestamps.get(partition);
                        // 设置分区的消费位置
                        resetOffsetIfNeeded(partition, requestedResetTimestamp, offsetData);
                    }
                }
);
        }
    }
    
    
    private void resetOffsetIfNeeded(TopicPartition partition, Long requestedResetTimestamp, OffsetData offsetData) {
        if (!subscriptions.isAssigned(partition)) {
            log.debug("Skipping reset of partition {} since it is no longer assigned", partition);
        } else if (!subscriptions.isOffsetResetNeeded(partition)) {
            log.debug("Skipping reset of partition {} since reset is no longer needed", partition);
        } else if (!requestedResetTimestamp.equals(offsetResetStrategyTimestamp(partition))) {
            log.debug("Skipping reset of partition {} since an alternative reset has been requested", partition);
        } else {
            log.info("Resetting offset for partition {} to offset {}.", partition, offsetData.offset);
            // 调用seek方法，设置分区的消费位置
            subscriptions.seek(partition, offsetData.offset);
        }
    }
                               
}
```



## 更改分区Offset

除了上面的Offset初始化，可以修改分区的消费offset。当KafkaConsumer从Fetcher里获取消息时，会更新分区的offset值。

Fetcher的fetchRecords方法，负责从指定分区中读取消息，并且调用SubscriptionState类的position方法，来更新分区offset。

```java
public class Fetcher<K, V> implements SubscriptionState.Listener, Closeable {
    private final SubscriptionState subscriptions;    // 分区信息
    
    private List<ConsumerRecord<K, V>> fetchRecords(PartitionRecords partitionRecords, int maxRecords) {
        if (!subscriptions.isAssigned(partitionRecords.partition)) {
           ......
        } else if (!subscriptions.isFetchable(partitionRecords.partition)) {
           ......
        } else {
            // 从SubscriptionState类，获取当前的分区offset
            long position = subscriptions.position(partitionRecords.partition);
            if (partitionRecords.nextFetchOffset == position) {
                // 从Fetcher缓存中，获取消息
                List<ConsumerRecord<K, V>> partRecords = partitionRecords.fetchRecords(maxRecords);
                long nextOffset = partitionRecords.nextFetchOffset;
                // 更新SubscriptionState类的分区offset
                subscriptions.position(partitionRecords.partition, nextOffset);

                ......

                return partRecords;
            } else {
              .....
            }
        }
        ......
    }
}
```



## 提交分区 Offset

KafkaConsumer提供了一系列的方法提交offset，支持同步和异步。

```java
public class KafkaConsumer<K, V> implements Consumer<K, V> {

    private final SubscriptionState subscriptions;
    private final ConsumerCoordinator coordinator;
    
    @Override
    public void commitAsync() {
        commitAsync(null);
    }

    @Override
    public void commitAsync(OffsetCommitCallback callback) {
        acquireAndEnsureOpen();
        try {
            // 从SubscriptionState获取分区的消费位置，然后提交
            commitAsync(subscriptions.allConsumed(), callback);
        } finally {
            release();
        }
    }

    @Override
    public void commitAsync(final Map<TopicPartition, OffsetAndMetadata> offsets, OffsetCommitCallback callback) {
        acquireAndEnsureOpen();
        try {
            // 调用ConsumerCoordinator的commitOffsetsAsync，向服务端发送提交请求
            coordinator.commitOffsetsAsync(new HashMap<>(offsets), callback);
        } finally {
            release();
        }
    }
    
    @Override
    public void commitSync(Duration timeout) {
        acquireAndEnsureOpen();
        try {
            // 调用ConsumerCoordinator的commitOffsetsAsync，向服务端发送提交请求
            if (!coordinator.commitOffsetsSync(subscriptions.allConsumed(), timeout.toMillis())) {
                throw new TimeoutException("Timeout of " + timeout.toMillis() + "ms expired before successfully " +
                        "committing the current consumed offsets");
            }
        } finally {
            release();
        }
    }
    
}
```



上述的方法，都是从SubscriptionState里获取分区消费位置，然后由ConsumerCoordinator发送请求。

```java
public ConsumerCoordinator(...) {
    
    private final ConsumerInterceptors<?, ?> interceptors; // 拦截器
    
    
    public boolean commitOffsetsSync(Map<TopicPartition, OffsetAndMetadata> offsets, long timeoutMs) {
        invokeCompletedOffsetCommitCallbacks();
        if (offsets.isEmpty())
            return true;

        long now = time.milliseconds();
        long startMs = now;
        long remainingMs = timeoutMs;
        do {
            if (coordinatorUnknown()) {
                if (!ensureCoordinatorReady(remainingMs))
                    return false;

                remainingMs = timeoutMs - (time.milliseconds() - startMs);
            }
            // 构造OffsetCommitRequest请求，发送给服务端GroupCoordinator
            RequestFuture<Void> future = sendOffsetCommitRequest(offsets);
            // 等待请求完成
            client.poll(future, remainingMs);

            invokeCompletedOffsetCommitCallbacks();

            if (future.succeeded()) {
                if (interceptors != null)
                    // 调用拦截器的onCommit回调
                    interceptors.onCommit(offsets);
                // 返回tur表示请求已经完成
                return true;
            }
            // 如果响应出错，那么抛出一样
            if (future.failed() && !future.isRetriable())
                throw future.exception();
            // 暂停一段时间，防止频繁操作
            time.sleep(retryBackoffMs);

            now = time.milliseconds();
            // 计算剩余时间
            remainingMs = timeoutMs - (now - startMs);
        } while (remainingMs > 0);
        // 返回false，表示请求还未完成
        return false;
    }
    
}
```



## 自动提交

如果consumer设置了自动提交，那么consumer会每次拉取消息的时候，将当前的offset提交。

KafkaConsumer在每次poll的时候，都会调用ConsumerCoordinator的poll方法。ConsumerCoordinator会定期检查是否到了该提交offset的时间，然后发送请求。

ConsumerCoordinator的poll方法，会调用maybeAutoCommitOffsetsAsync方法，负责提交请求

```java
public final class ConsumerCoordinator extends AbstractCoordinator {
    private final SubscriptionState subscriptions;  // 分区信息
    private final boolean autoCommitEnabled;  // 是否允许自动提交
    private final int autoCommitIntervalMs;  // 自动提交的时间间隔 
    private long nextAutoCommitDeadline;   // 下次自动提交时间
    
    private final ConcurrentLinkedQueue<OffsetCommitCompletion> completedOffsetCommits;    
    
    
    public void maybeAutoCommitOffsetsAsync(long now) {
        // 如果允许自动提交，并且时间到了自动提交的时间
        if (autoCommitEnabled && now >= nextAutoCommitDeadline) {
            // 更新下次提交时间
            this.nextAutoCommitDeadline = now + autoCommitIntervalMs;
            // 提交offset
            doAutoCommitOffsetsAsync();
        }
    }
    
    private void doAutoCommitOffsetsAsync() {
        // 从SubscriptionState获取消费位置信息
        Map<TopicPartition, OffsetAndMetadata> allConsumedOffsets = subscriptions.allConsumed();
        // 提交请求
        commitOffsetsAsync(allConsumedOffsets, new OffsetCommitCallback() {
            ......
        });
    }

    public void commitOffsetsAsync(final Map<TopicPartition, OffsetAndMetadata> offsets, final OffsetCommitCallback callback) {
        invokeCompletedOffsetCommitCallbacks();

        if (!coordinatorUnknown()) {
            // 保证与服务端的GroupCoordinator连接是好的，然后发送请求
            doCommitOffsetsAsync(offsets, callback);
        } else { 
            .....
        }

        client.pollNoWakeup();
    }

    private void doCommitOffsetsAsync(final Map<TopicPartition, OffsetAndMetadata> offsets, final OffsetCommitCallback callback) {
        // 发送请求
        RequestFuture<Void> future = sendOffsetCommitRequest(offsets);
        final OffsetCommitCallback cb = callback == null ? defaultOffsetCommitCallback : callback;
        future.addListener(new RequestFutureListener<Void>() {
            @Override
            public void onSuccess(Void value) {
                if (interceptors != null)
                    // 调用拦截器的onCommit回调函数
                    interceptors.onCommit(offsets);
                // 将回调添加到队列里，等待后续执行
                completedOffsetCommits.add(new OffsetCommitCompletion(cb, offsets, null));
            }

            @Override
            public void onFailure(RuntimeException e) {
                Exception commitException = e;
                if (e instanceof RetriableException)
                    commitException = new RetriableCommitFailedException(e);
                // 将回调添加到队列里，等待后续执行
                completedOffsetCommits.add(new OffsetCommitCompletion(cb, offsets, commitException));
            }
        });
    }
}
```



这里额外说下使用自动提交的注意点。KafkaConsumer拉取消息时，会先自动向服务端提交offset请求。之后才真正的从Fetcher里拉取数据，而Fetcher在返回数据时，会更新分区的offset。所以如果开启了自动提交，在每次拉取消息后，一定要保证已经处理完这些消息，才去拉取新的消息。



## 执行任务时间过长

我们调用 poll 方法获取到消息之后，会立即处理请求。有时候消息的处理会比较久，导致两次 poll 之间的时间间隔太长，就会引起 rebalance 操作。为了避免这种情况，可以设置`max.poll.records`配置项，设置单次 poll 获取的消息。