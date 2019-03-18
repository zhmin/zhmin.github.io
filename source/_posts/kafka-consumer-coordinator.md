---
title: KafkaConsumer Coordinator 原理
date: 2019-03-18 22:11:55
tags: kafka
categories: kafka
---

# KafkaConsumer  Coordinator 原理

Kafka消费者提供了组的概念，它允许多个consumer共同消费一个topic，而不会造成冲突。Kafka提供了Coordinator服务，负责管理消费组。当有新增的consumer加入组，或者有consumer离开组，都会触发Coordinator的重新平衡操作，Coordinator会将topic的分区重新分配给各个consumer。

新的consumer加入到组的过程如下：

1. consumer首先从Kafka集群中选择出一个节点，并且发出请求，寻找Coordinator的地址
2. 获取Coordinator的地址后，consumer向Coordinator发送请求加入，请求会包含自己的订阅信息
3. Coordinator会为每个consumer分配 id 号，并且从中选择出leader角色。
4. consumer收到响应后，如果是被选择是leader角色，那么执行分配算法，将所有consumer的分区分配结果发送给Coordinator。如果是follower角色，那么向Coordinator发送请求获取该consumer的分配结果。



## 寻找Coordinator地址

consumer第一步是需要找到Coordinator的地址，才能进行后续的请求。它从Kafka集群中选择出一个负载最轻的节点，并且发出寻找Coordinator地址的请求。

### 协议格式

请求格式的主要字段：

| 字段名           | 字段类型 | 字段含义                     |
| ---------------- | -------- | ---------------------------- |
| coordinator_key  | 字符串   | group id 或者 transaction id |
| coordinator_type | 字符串   | coordinator_key是哪一种类型  |

响应格式的主要字段：

| 字段名  | 字段类型 | 字段含义                        |
| ------- | -------- | ------------------------------- |
| node_id | 字符串   | coordinator服务所在主机的 id 号 |
| host    | 字符串   | 主机地址                        |
| port    | 整数     | 服务端口号                      |



### 源码分析

AbstractCoordinator的ensureCoordinatorReady会发送寻找Coordinator的请求，并且创建连接。

```java
public abstract class AbstractCoordinator implements Closeable {
    
    protected final ConsumerNetworkClient client;    
    
    protected synchronized boolean ensureCoordinatorReady(final long timeoutMs) {
        final long startTimeMs = time.milliseconds();
        long elapsedTime = 0L;
        // 调用coordinatorUnknown方法，检测Coordinator地址是否已经获取了
        while (coordinatorUnknown()) {
            // 发送寻找Coordinator的请求
            final RequestFuture<Void> future = lookupCoordinator();
            // 等待响应完成，remainingTimeAtLeastZero方法计算等待时长
            client.poll(future, remainingTimeAtLeastZero(timeoutMs, elapsedTime));
            if (!future.isDone()) {
                // 如果超时，还没有收到响应
                break;
            }

            if (future.failed()) {
                // 检测是否可以重试
                if (future.isRetriable()) {
                    elapsedTime = time.milliseconds() - startTimeMs;
                    if (elapsedTime >= timeoutMs) break;
                    // 更新元数据并且等待完成
                    client.awaitMetadataUpdate(remainingTimeAtLeastZero(timeoutMs, elapsedTime));
                    elapsedTime = time.milliseconds() - startTimeMs;
                } else
                    throw future.exception();
            } else if (coordinator != null && client.isUnavailable(coordinator)) {
                // 虽然找到了Coordinator地址，但是连接失败
                markCoordinatorUnknown();
                final long sleepTime = Math.min(retryBackoffMs, remainingTimeAtLeastZero(timeoutMs, elapsedTime));
                time.sleep(sleepTime);
                elapsedTime += sleepTime;
            }
        }
        // 返回是否与Coordinator建立连接
        return !coordinatorUnknown();
    }
}
```



注意到上面的lookupCoordinator方法，它负责构建和发送请求。

```java
public abstract class AbstractCoordinator implements Closeable {
    
    protected final ConsumerNetworkClient client;
    private RequestFuture<Void> findCoordinatorFuture = null;
    

    protected synchronized RequestFuture<Void> lookupCoordinator() {
        if (findCoordinatorFuture == null) {
            // 找到负载最轻的节点
            Node node = this.client.leastLoadedNode();
            if (node == null) {
                return RequestFuture.noBrokersAvailable();
            } else
                // 发送请求
                findCoordinatorFuture = sendFindCoordinatorRequest(node);
        }
        return findCoordinatorFuture;
    }
    
    private RequestFuture<Void> sendFindCoordinatorRequest(Node node) {
        // 构建请求
        FindCoordinatorRequest.Builder requestBuilder =
                new FindCoordinatorRequest.Builder(FindCoordinatorRequest.CoordinatorType.GROUP, this.groupId);
        // 这里先调用了client的send方法，返回RequestFuture<ClientResponse>类型
        // 然后调用了compose方法，转换为RequestFuture<Void>类型
        return client.send(node, requestBuilder)
                     .compose(new FindCoordinatorResponseHandler());
    } 
}
```

上面涉及到了RequestFuture类型的转换，关于RequestFuture的原理，可以参考这篇博客 {% post_link kafka-consumer-network-client %} 。FindCoordinatorResponseHandler在响应完成时，会保存coordinator地址，并且创建与coordinator的连接。

```java
private class FindCoordinatorResponseHandler extends RequestFutureAdapter<ClientResponse, Void> {
    
    @Override
    public void onSuccess(ClientResponse resp, RequestFuture<Void> future) {
        clearFindCoordinatorFuture();
        // 强制转换为FindCoordinatorResponse类型
        FindCoordinatorResponse findCoordinatorResponse = (FindCoordinatorResponse) resp.responseBody();
        // 查看响应是否有错误
        Errors error = findCoordinatorResponse.error();
        if (error == Errors.NONE) {
            synchronized (AbstractCoordinator.this) {
                // use MAX_VALUE - node.id as the coordinator id to allow separate connections
                // for the coordinator in the underlying network client layer
                int coordinatorConnectionId = Integer.MAX_VALUE - findCoordinatorResponse.node().id();
                // 保存coordinator地址
                AbstractCoordinator.this.coordinator = new Node(
                        coordinatorConnectionId,
                        findCoordinatorResponse.node().host(),
                        findCoordinatorResponse.node().port());
                // 连接 Coordinator节点
                client.tryConnect(coordinator);
                // 设置心跳的超时时间
                heartbeat.resetTimeouts(time.milliseconds());
            }
            // 设置future的结果
            future.complete(null);
        } else if (error == Errors.GROUP_AUTHORIZATION_FAILED) {
            // 设置future的异常
            future.raise(new GroupAuthorizationException(groupId));
        } else {
            // 设置future的异常
            future.raise(error);
        }
    }
```



## 请求加入组

consumer在连接coordinator之后，会与它进行请求交互。它首先会发送加入组的请求，coordinator会分配consumer id，并且会从组中选出leader角色和follower角色。

### 协议格式

请求格式的主要字段：

| 字段名            | 字段类型                | 字段含义                               |
| ----------------- | ----------------------- | -------------------------------------- |
| group_id          | 字符串                  | consumer 所在的 group id               |
| session_timeout   | 整数                    | 心跳超时时间                           |
| rebalance_timeout | 整数                    | rebalance超时时间                      |
| protocol_type     | 字符串                  | coordinator协议名称，默认是 ”consumer“ |
| group_protocols   | group_protocol 类型列表 | group_protocol 类型列表                |

group_protocol 数据格式

| 字段名            | 字段类型 | 字段含义                       |
| ----------------- | -------- | ------------------------------ |
| protocol_name     | 字符串   | consumer支持的分配算法的名称   |
| protocol_metadata | 字节数组 | consumer对于此算法的自定义数据 |



响应格式的主要字段：

| 字段名         | 字段类型        | 字段含义               |
| -------------- | --------------- | ---------------------- |
| error_code     | 整数            | 错误码                 |
| generation_id  | 字符串          | 表示coordinator的版本  |
| group_protocol | 字符串          | 选用的分配算法名称     |
| leader_id      | 整数            | leader角色的 id 号     |
| member_id      | 整数            | 该consumer 的 id 号    |
| members        | member 类型列表 | 所有consumer的订阅信息 |

member 数据格式

| 字段名          | 字段类型 | 字段含义             |
| --------------- | -------- | -------------------- |
| member_id       | 整数     | consumer 的 id 号    |
| member_metadata | 字节数组 | consumer自定义的数据 |



### 源码分析

initiateJoinGroup方法会负责与Coordinator的请求交互

```java
private synchronized RequestFuture<ByteBuffer> initiateJoinGroup() {
    if (joinFuture == null) {
        // 暂停心跳线程
        disableHeartbeatThread();

        state = MemberState.REBALANCING;
        // 发送加入group请求，返回RequestFuture
        joinFuture = sendJoinGroupRequest();
        // 添加监听器，当接收到响应后，会继续启动心跳线程
        joinFuture.addListener(new RequestFutureListener<ByteBuffer>() {
            @Override
            public void onSuccess(ByteBuffer value) {
                synchronized (AbstractCoordinator.this) {
                    //更新状态
                    state = MemberState.STABLE;
                    // 设置rejoinNeeded为false，因为join请求已经完成了
                    rejoinNeeded = false;
                    if (heartbeatThread != null)
                        // 启动心跳线程
                        heartbeatThread.enable();
                }
            }

            @Override
            public void onFailure(RuntimeException e) {
                synchronized (AbstractCoordinator.this) {
                    // 设置状态
                    state = MemberState.UNJOINED;
                }
            }
        });
    }
    return joinFuture;
}

RequestFuture<ByteBuffer> sendJoinGroupRequest() {

    // 构建请求
    JoinGroupRequest.Builder requestBuilder = new JoinGroupRequest.Builder(
        groupId,
        this.sessionTimeoutMs,
        this.generation.memberId,
        protocolType(),  // 返回protocol_type，这里是”consumer“
        metadata()).setRebalanceTimeout(this.rebalanceTimeoutMs);  // metadata方法返回分配算法的信息

    int joinGroupTimeoutMs = Math.max(rebalanceTimeoutMs, rebalanceTimeoutMs + 5000);
    // 调用client的send发送请求，返回 RequestFuture<ClientResponse>
    // 这里调用compose方法，转换为 RequestFuture<ByteBuffer>
    return client.send(coordinator, requestBuilder, joinGroupTimeoutMs)
        .compose(new JoinGroupResponseHandler());
}
```



JoinGroupResponseHandler，会比较响应内容的memberId和leaderId字段，如果两者相同则表示这个consumer是leader角色，否则就是follower角色。leader角色会执行分区的分配算法，follower角色会去请求分配的结果。

```java
private class JoinGroupResponseHandler extends CoordinatorResponseHandler<JoinGroupResponse, ByteBuffer> {
    
    @Override
    public void handle(JoinGroupResponse joinResponse, RequestFuture<ByteBuffer> future) {
        Errors error = joinResponse.error();
        if (error == Errors.NONE) {
            synchronized (AbstractCoordinator.this) {
                if (state != MemberState.REBALANCING) {
                    // 检查是否为REBALANCING状态，那么说明需要重新加入group，所以这次请求响应需要退出
                    future.raise(new UnjoinedGroupException());
                } else {
                    // 更新generation数据，从响应中获取generationId，memberId和groupProtocol
                    AbstractCoordinator.this.generation = new Generation(joinResponse.generationId(),
                            joinResponse.memberId(), joinResponse.groupProtocol());
                    // 如果这个consumer被认为是leader角色，那么调用onJoinLeader执行分区分配
                    if (joinResponse.isLeader()) {
                        // 注意到这里使用了chain，当onJoinLeader的结果完成后，才会调用future完成
                        onJoinLeader(joinResponse).chain(future);
                    } else {
                        // 如果这个consumer被认为是follower角色，那么调用onJoinFollower获取分配结果
                        // 注意到这里使用了chain，当onJoinFollower的结果完成后，才会调用future完成
                        onJoinFollower().chain(future);
                    }
                }
            }
        } else if (error ) {
            // 处理各种错误
            ......
            future.raise(error);
        }
    }
}
```



## leader执行分配

Coordinator会为组里的每个consumer分配 id 号，并且从组里选择出leader角色。Coordinator返回给leader角色的响应中，包含了分配算法和组里所有consumer的订阅信息。leader角色收到响应后，会执行分区的分配算法，然后将结果保存到group_assignment字段里，发送给Coordinator。

### 协议格式

请求格式的主要字段：

| 字段名           | 字段类型            | 字段含义                 |
| ---------------- | ------------------- | ------------------------ |
| group_id         | 字符串              | consumer 所在的 group id |
| generation_id    | 整数                | coordinator的版本号      |
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



### 源码分析

onJoinLeader方法负责执行分配算法，关于分配算法的原理可以参考后续博客

```java
private RequestFuture<ByteBuffer> onJoinLeader(JoinGroupResponse joinResponse) {
    try {
        // 子类负责实现performAssignment方法，分区分配
        // 返回结果的格式，key为consumer id，value为对应consumer的分配结果，这里已经将结果序列化了
        Map<String, ByteBuffer> groupAssignment = performAssignment(joinResponse.leaderId(), joinResponse.groupProtocol(),
                joinResponse.members());
        // 构建SyncGroupRequest请求
        SyncGroupRequest.Builder requestBuilder =
                new SyncGroupRequest.Builder(groupId, generation.generationId, generation.memberId, groupAssignment);
        // 发送SyncGroupRequest请求
        return sendSyncGroupRequest(requestBuilder);
    } catch (RuntimeException e) {
        return RequestFuture.failure(e);
    }
}

private RequestFuture<ByteBuffer> sendSyncGroupRequest(SyncGroupRequest.Builder requestBuilder) {
    if (coordinatorUnknown())
        return RequestFuture.coordinatorNotAvailable();
    // 发送响应，这里涉及到SyncGroupResponseHandler回调
    return client.send(coordinator, requestBuilder)
            .compose(new SyncGroupResponseHandler());
}
```

SyncGroupResponseHandler将分配的结果保存到RequestFuture里

```java
private class SyncGroupResponseHandler extends CoordinatorResponseHandler<SyncGroupResponse, ByteBuffer> {
    
    @Override
    public void handle(SyncGroupResponse syncResponse,
                       RequestFuture<ByteBuffer> future) {
        Errors error = syncResponse.error();
        if (error == Errors.NONE) {
            // 将分配的结果保存到future里
            future.complete(syncResponse.memberAssignment());
        } else {
            // 处理各种异常
            ........
        }
    }
}
```



## follower请求分配结果

follower角色同样发送了sendSyncGroupRequest请求，不过它的groupAssignments字段是空的。剩下的处理响应过程，和leader角色一样。

```java
private RequestFuture<ByteBuffer> onJoinFollower() {
    // 因为是follower角色，并不负责分区分配，所以groupAssignments字段为空
    SyncGroupRequest.Builder requestBuilder =
            new SyncGroupRequest.Builder(groupId, generation.generationId, generation.memberId,
                    Collections.<String, ByteBuffer>emptyMap());
    // 调用sendSyncGroupRequest方法，发送请求，原理同leader一样
    return sendSyncGroupRequest(requestBuilder);
}
```



## 心跳线程

consumer会启动一个线程，这个线程会定时的向Coordinator发送心跳请求，来通知Coordinator自己还活着。如果Coordinator没有收到心跳信息，那么它就会认为该consumer已经挂了。

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



### 源码分析

心跳线程有三种状态，运行，暂停和关闭。它将每次心跳时间信息，都会保存到Heartbeat类。线程一直循环检测，现在是否到了需要发送心跳的时间。

```java
public abstract class AbstractCoordinator implements Closeable {
    // 与coordinator之间的状态
    private MemberState state = MemberState.UNJOINED;
    
    private class HeartbeatThread extends KafkaThread {
        // 如果为false，表示暂停状态
        // 如果为true，表示运行状态
        private boolean enabled = false;
        // 是否为关闭状态
        private boolean closed = false;
        private AtomicReference<RuntimeException> failed = new AtomicReference<>(null);
        
        @Override
        public void run() {
            try {
                while (true) {
                    synchronized (AbstractCoordinator.this) {
                        // 如果是关闭状态，则退出
                        if (closed)
                            return;
                        // 如果是暂停状态，则等待
                        if (!enabled) {
                            AbstractCoordinator.this.wait();
                            continue;
                        }

                        if (state != MemberState.STABLE) {
                            // 如果与coordinator的连接状态有问题，则进入暂停状态
                            disable();
                            continue;
                        }
                        // 通知ConsumerNetworkClient，防止它阻塞
                        client.pollNoWakeup();
                        long now = time.milliseconds();

                        if (coordinatorUnknown()) {
                            if (findCoordinatorFuture != null || lookupCoordinator().failed())
                                // 检查是否找到
                                AbstractCoordinator.this.wait(retryBackoffMs);
                        } else if (heartbeat.sessionTimeoutExpired(now)) {
                            // 如果第一次心跳超时，则认为与coordinator的连接失败
                            markCoordinatorUnknown();
                        } else if (heartbeat.pollTimeoutExpired(now)) {
                            // 如果心跳超时，则认为与coordinator的连接失败，需要退出组
                            maybeLeaveGroup();
                        } else if (!heartbeat.shouldHeartbeat(now)) {
                            // 心跳发送必须保持一定的间隔，这里检查是否能发送
                            AbstractCoordinator.this.wait(retryBackoffMs);
                        } else {
                            // 设置最新发送心跳的时间
                            heartbeat.sentHeartbeat(now);
                            // 发送心跳
                            sendHeartbeatRequest().addListener(new RequestFutureListener<Void>() {
                                @Override
                                public void onSuccess(Void value) {
                                    synchronized (AbstractCoordinator.this) {
                                        // 设置最新接收心跳的时间
                                        heartbeat.receiveHeartbeat(time.milliseconds());
                                    }
                                }

                                @Override
                                public void onFailure(RuntimeException e) {
                                    synchronized (AbstractCoordinator.this) {
                                        if (e instanceof RebalanceInProgressException) {
                                            // 接收到Rebalance异常，这个coordinator正在处在reblance状态
                                            heartbeat.receiveHeartbeat(time.milliseconds());
                                        } else {
                                            heartbeat.failHeartbeat();
                                            AbstractCoordinator.this.notify();
                                        }
                                    }
                                }
                            });
                        }
                    }
                }
            } catch (...) {
                // 处理各种异常
                ....
                this.failed.set(e);
            } 
        }

    }        
}
```



## ConsumerCoordinator 原理

ConsumerCoordinator类继承AbstractCoordinator类，实现了几个关键的回调函数。

- onJoinPrepare，在发送加入请求之前

- onJoinComplete，在获得分配结果之后

在介绍源码之前，需要提下SubscriptionState类，它包含了订阅信息和订阅结果。当consumer是leader角色时，它还包含了这个消费组订阅的所有topic。

```java
public final class ConsumerCoordinator extends AbstractCoordinator {
    // 保存了上次分配结果中，所有分区涉及到的topic
    private Set<String> joinedSubscription;
    // 保存了分配结果
    private final SubscriptionState subscriptions;
    
    @Override
    protected void onJoinPrepare(int generation, String memberId) {
        // 及时提交offset
        maybeAutoCommitOffsetsSync(rebalanceTimeoutMs);

        // 如果有关于重平衡的监听器，需要执行它的回调
        ConsumerRebalanceListener listener = subscriptions.rebalanceListener();
        try {
            Set<TopicPartition> revoked = new HashSet<>(subscriptions.assignedPartitions());
            listener.onPartitionsRevoked(revoked);
        } catch (WakeupException | InterruptException e) {
            throw e;
        } catch (Exception e) {
            ...
        }
        // 因为每次加入组，都是由Coordinator负责选出leader角色的
        isLeader = false;
        // 重置消费组订阅的topic列表，为当前consumer的订阅列表
        subscriptions.resetGroupSubscription();
    }
    
    
    @Override
    protected void onJoinComplete(int generation,
                                  String memberId,
                                  String assignmentStrategy,
                                  ByteBuffer assignmentBuffer) {
        // 只有leader角色，才会监听分配结果的变化
        if (!isLeader)
            assignmentSnapshot = null;

        PartitionAssignor assignor = lookupAssignor(assignmentStrategy);
        if (assignor == null)
            throw new IllegalStateException("Coordinator selected invalid assignment protocol: " + assignmentStrategy);
        // 解析响应数据，生成Assignment
        Assignment assignment = ConsumerProtocol.deserializeAssignment(assignmentBuffer);
        // 保存分区分配的结果，到subscriptions里
        subscriptions.assignFromSubscribed(assignment.partitions());

        // 检查有哪些新topic
        Set<String> addedTopics = new HashSet<>();
        for (TopicPartition tp : subscriptions.assignedPartitions()) {
            if (!joinedSubscription.contains(tp.topic()))
                addedTopics.add(tp.topic());
        }
        // 当有新的topic时，说明只有订阅的模式是正则匹配，才会有新的topic
        if (!addedTopics.isEmpty()) {
            Set<String> newSubscription = new HashSet<>(subscriptions.subscription());
            Set<String> newJoinedSubscription = new HashSet<>(joinedSubscription);
            newSubscription.addAll(addedTopics);
            newJoinedSubscription.addAll(addedTopics);
            // 更新订阅信息
            this.subscriptions.subscribeFromPattern(newSubscription);
            this.joinedSubscription = newJoinedSubscription;
        }
        // 添加这些topic到元数据里
        this.metadata.setTopics(subscriptions.groupSubscription());

        // 执行assignor的回调函数
        assignor.onAssignment(assignment);

        // 设置自动提交offset的下次时间
        this.nextAutoCommitDeadline = time.milliseconds() + autoCommitIntervalMs;

        // 执行相应的监听回调函数
        ConsumerRebalanceListener listener = subscriptions.rebalanceListener();
        try {
            Set<TopicPartition> assigned = new HashSet<>(subscriptions.assignedPartitions());
            listener.onPartitionsAssigned(assigned);
        } catch (WakeupException | InterruptException e) {
            throw e;
        } catch (Exception e) {
            ...
        }
    }    
}
```