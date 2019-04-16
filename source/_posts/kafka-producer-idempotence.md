---
title: Kafka Producer 幂等性原理
date: 2019-04-16 21:17:53
tags: kafka, producer, idempotence
categories: kafka
---

# Kafka Producer 幂等性原理

幂等性是指发送同样的请求，对系统资源的影响是一致的。结合 Kafka Producer，是指在多次发送同样的消息，Kafka做到发送消息的不丢失和不重复。实现幂等性服务，需要客户端和服务端的相互配合。客户端每次发送请求，需要得到服务端的确认，才认为此次请求成功。不然客户端只能不断的重试，来保证服务端不丢失请求。而这样服务端有可能收到重复的请求，所以需要对这些请求去重。比如Kafka Producer与服务端的网络异常：

- Producer向服务端发送消息，但是此时连接就断开了，发送的消息经过网络传输时，被丢失了。服务端没有接收到消息。
- 服务端收到Producer发送的消息，处理完毕后，向Producer发送响应，但是此时连接断开了。发送的响应经过网络传输时，被丢失了。Producer没有收到响应。

因为两种情况对于Producer而言，都是没有收到响应，Producer无法确定是哪种情况，所以它必须要重新发送消息，来确保服务端不会漏掉一条消息。但这样服务端有可能会收到重复的消息，所以服务端收到消息后，还要做一次去重操作。只有Producer和服务端的相互配合，才能保证消息不丢失也不重复，达到 Exactly One 的情景。

Kafka的幂等性只支持单个producer向单个分区发送。它会为每个Producer生成一个唯一id号，这样Kafka服务端就可以根据 produce_id来确定是哪个生产者发送的消息。然后Kafka还为每条消息生成了一个序列号，Kafka服务端会保留最近的消息，根据序列号就可以判断该消息，是否近期已经发送过，来达到去重的效果。



## Producer 发送消息

### 配置幂等性

KafkaProducer 如果要使用幂等性，需要将 enable.idempotence 配置项设置为true。并且它对单个分区的发送，一次性最多发送5条。通过KafkaProducer的 configureInflightRequests 方法，可以看到对max.in.flight.requests.per.connection的限制。

因为客户端的每次请求，都需要服务端的确认，所以KafkaProducer在开启幂等性后，需要设置 ack。

```java
public class KafkaProducer<K, V> implements Producer<K, V> {

    private static int configureInflightRequests(ProducerConfig config, boolean idempotenceEnabled) {
        if (idempotenceEnabled && 5 < config.getInt(ProducerConfig.MAX_IN_FLIGHT_REQUESTS_PER_CONNECTION)) {
            throw new ConfigException("Must set " + ProducerConfig.MAX_IN_FLIGHT_REQUESTS_PER_CONNECTION + " to at most 5" +
                    " to use the idempotent producer.");
        }
        return config.getInt(ProducerConfig.MAX_IN_FLIGHT_REQUESTS_PER_CONNECTION);
    }
    
    
    private static short configureAcks(ProducerConfig config, boolean idempotenceEnabled, Logger log) {
        boolean userConfiguredAcks = false;
        short acks = (short) parseAcks(config.getString(ProducerConfig.ACKS_CONFIG));
        if (config.originals().containsKey(ProducerConfig.ACKS_CONFIG)) {
            userConfiguredAcks = true;
        }
        // 如果开启了幂等性，但是用户没有指定ack，则返回 -1。-1表示包括leader和follower分区都要确认
        if (idempotenceEnabled && !userConfiguredAcks) {
            return -1;
        }
        // 如果开启了幂等性，但是用户指定的ack不为 -1，则会抛出异常
        if (idempotenceEnabled && acks != -1) {
            throw new ConfigException(".....");
        }
        return acks;
    }    
}
```



### 请求获取 producer id

如果Producer开启了幂等性，那么Sender在发送消息之前，都会去检查是否已经获取到了 produce_id。如果没有，会向服务端发送请求，然后保存在TransactionManager类里。

```java
public class Sender implements Runnable {

    private void maybeWaitForProducerId() {
        // 判断是否获取了produce_id
        while (!transactionManager.hasProducerId() && !transactionManager.hasError()) {
            try {
                // 选择负载最轻的一台节点
                Node node = awaitLeastLoadedNodeReady(requestTimeoutMs);
                if (node != null) {
                    // 发送获取请求，接收响应
                    ClientResponse response = sendAndAwaitInitProducerIdRequest(node);
                    // 处理响应
                    InitProducerIdResponse initProducerIdResponse = (InitProducerIdResponse) response.responseBody();
                    // 检查错误
                    Errors error = initProducerIdResponse.error();
                    if (error == Errors.NONE) {
                        ProducerIdAndEpoch producerIdAndEpoch = new ProducerIdAndEpoch(
                                initProducerIdResponse.producerId(), initProducerIdResponse.epoch());
                        // 保存produce_id 和 produce_epoch结果（produce_epoch是在开启事务才会用到，这里仅仅是开启了幂等性，没有用到事务）
                        transactionManager.setProducerIdAndEpoch(producerIdAndEpoch);
                        return;
                    } else if (error.exception() instanceof RetriableException) {
                        // 如果该错误是可以重试，那么就等待下次重试
                        log.debug("Retriable error from InitProducerId response", error.message());
                    } else {
                        // 如果发生严重错误，那么保存错误信息，并且退出
                        transactionManager.transitionToFatalError(error.exception());
                        break;
                    }
                } else {
                   .....
                }
            } catch (UnsupportedVersionException e) {
                // 发生版本不支持的错误，则退出
                transactionManager.transitionToFatalError(e);
                break;
            } catch (IOException e) {
                // 发生网络通信问题，则等待下次重试
                log.debug("Broker {} disconnected while awaiting InitProducerId response", e);
            }
            // 等待一段时间，然后重试
            time.sleep(retryBackoffMs);
            metadata.requestUpdate();
        }
    }
    
    private ClientResponse sendAndAwaitInitProducerIdRequest(Node node) throws IOException {
        String nodeId = node.idString();
        // 构建InitProducerIdRequest请求
        InitProducerIdRequest.Builder builder = new InitProducerIdRequest.Builder(null);
        ClientRequest request = client.newClientRequest(nodeId, builder, time.milliseconds(), true, requestTimeoutMs, null);
        // 发送请求并且等待响应
        return NetworkClientUtils.sendAndReceive(client, request, time);
    }    
}
```



### 配置消息

当开启了幂等性，KafkaProducer发送消息时，会额外设置producer_id 和 序列号字段。producer_id是从Kafka服务端请求获取的，消息序列号是Producer端生成的，初始值为0，之后自增加一。这里需要说明下，Kafka发送消息都是以batch的格式发送，batch包含了多条消息。所以Producer发送消息batch的时候，只会设置该batch的第一个消息的序列号，后面消息的序列号可以根据第一个消息的序列号计算出来。

当Sender从RecordAccumulator中拉取消息时，会设置produce_id 和 baseSequence两个字段。

```java
public final class RecordAccumulator {
    
    public Map<Integer, List<ProducerBatch>> drain(Cluster cluster, Set<Node> nodes, int maxSize, long now) {
        if (nodes.isEmpty())
            return Collections.emptyMap();

        Map<Integer, List<ProducerBatch>> batches = new HashMap<>();
        for (Node node : nodes) {
            int size = 0;
            List<PartitionInfo> parts = cluster.partitionsForNode(node.id());
            List<ProducerBatch> ready = new ArrayList<>();
            int start = drainIndex = drainIndex % parts.size();
            do {
                PartitionInfo part = parts.get(drainIndex);
                TopicPartition tp = new TopicPartition(part.topic(), part.partition());
                // 当max.in.flight.requests.per.connection配置项为 1 时，Sender发送消息的时候，会暂时关闭此分区的请求发送。当完成响应时，才会开放请求发送。
                // 这里的isMute方法，是用来判断次分区的请求是否被关闭
                if (!isMuted(tp, now)) {
                    Deque<ProducerBatch> deque = getDeque(tp);
                    if (deque != null) {
                        synchronized (deque) {
                            ProducerBatch first = deque.peekFirst();
                            if (first != null) {
                                boolean backoff = first.attempts() > 0 && first.waitedTimeMs(now) < retryBackoffMs;
                                // Only drain the batch if it is not during backoff period.
                                if (!backoff) {
                                    if (size + first.estimatedSizeInBytes() > maxSize && !ready.isEmpty()) {
                                        break;
                                    } else {
                                        ProducerIdAndEpoch producerIdAndEpoch = null;
                                        boolean isTransactional = false;
                                        if (transactionManager != null) {
                                            // 判断是否可以向这个分区发送请求
                                            if (!transactionManager.isSendToPartitionAllowed(tp))
                                                break;
                                            // 获取producer_id 和 epoch
                                            producerIdAndEpoch = transactionManager.producerIdAndEpoch();
                                            if (!producerIdAndEpoch.isValid())
                                                // we cannot send the batch until we have refreshed the producer id
                                                break;
                                            // 这里判断是否开启了事务
                                            isTransactional = transactionManager.isTransactional();
                                            // 如果这个消息batch，已经设置了序列号，并且此分区连接有问题， 那么需要跳过这个消息batch
                                            if (!first.hasSequence() && transactionManager.hasUnresolvedSequence(first.topicPartition))
                                                break;
                                            // 查看消息batch是否重试，如果是，则跳过
                                            int firstInFlightSequence = transactionManager.firstInFlightSequence(first.topicPartition);
                                            if (firstInFlightSequence != RecordBatch.NO_SEQUENCE && first.hasSequence()
                                                    && first.baseSequence() != firstInFlightSequence)

                                                break;
                                        }

                                        ProducerBatch batch = deque.pollFirst();
                                        // 为新的消息batch，设置对应的字段值
                                        if (producerIdAndEpoch != null && !batch.hasSequence()) {
                                            // 这里调用了transactionManager生成序列号
                                            batch.setProducerState(producerIdAndEpoch, transactionManager.sequenceNumber(batch.topicPartition), isTransactional);
                                            // 更新序列号
                                            transactionManager.incrementSequenceNumber(batch.topicPartition, batch.recordCount);

                                            transactionManager.addInFlightBatch(batch);
                                        }
                                        batch.close();
                                        size += batch.records().sizeInBytes();
                                        ready.add(batch);
                                        batch.drained(now);
                                    }
                                }
                            }
                        }
                    }
                }
                this.drainIndex = (this.drainIndex + 1) % parts.size();
            } while (start != drainIndex);
            batches.put(node.id(), ready);
        }
        return batches;
    }
}


```



从上面可以看到，TransactionManager负责为每个发送的消息batch，都会生成序列号。每个分区都有独立的序列号。

```java
public class TransactionManager {
    // 为每个分区，维护一个消息序列号
    private final Map<TopicPartition, Integer> nextSequence;

    synchronized Integer sequenceNumber(TopicPartition topicPartition) {
        Integer currentSequenceNumber = nextSequence.get(topicPartition);
        if (currentSequenceNumber == null) {
            // 初始序列号为 0
            currentSequenceNumber = 0;
            nextSequence.put(topicPartition, currentSequenceNumber);
        }
        return currentSequenceNumber;
    }

    
    synchronized void incrementSequenceNumber(TopicPartition topicPartition, int increment) {
        Integer currentSequenceNumber = nextSequence.get(topicPartition);
        if (currentSequenceNumber == null)
            throw new IllegalStateException("Attempt to increment sequence number for a partition with no current sequence.");
        // 更新分区对应的序列号
        currentSequenceNumber += increment;
        nextSequence.put(topicPartition, currentSequenceNumber);
    }
}
```



## 服务端处理请求

Kafka服务端接收到请求后，会调用ReplicaManager的方法处理。ReplicaManager最后通过Log类，检测请求的有效性，然后存储起来。Log类的 analyzeAndValidateProducerState 方法，负责检测消息的序列号是否有效。

```scala
class Log(...) {
    
  private def analyzeAndValidateProducerState(records: MemoryRecords, isFromClient: Boolean):
  (mutable.Map[Long, ProducerAppendInfo], List[CompletedTxn], Option[BatchMetadata]) = {
    // 添加信息的表，Key值为produce_id，Value为添加信息，它包含了新添加的消息batch
    val updatedProducers = mutable.Map.empty[Long, ProducerAppendInfo]
    val completedTxns = ListBuffer.empty[CompletedTxn]
    // 遍历消息 batch
    for (batch <- records.batches.asScala if batch.hasProducerId) {
      // 根据producer_id找到，对应producer发送的最近请求
      val maybeLastEntry = producerStateManager.lastEntry(batch.producerId)

      // 这里的请求有可能来自客户端，也有可能是从leader分区向follower分区发来的
      // if this is a client produce request, there will be up to 5 batches which could have been duplicated.
      // If we find a duplicate, we return the metadata of the appended batch to the client.
      if (isFromClient) {
        // 检测是否有近期重复的请求，如果有则立马返回
        maybeLastEntry.flatMap(_.findDuplicateBatch(batch)).foreach { duplicate =>
          return (updatedProducers, completedTxns.toList, Some(duplicate))
        }
      }
      // 将消息batch的添加信息，添加到updatedProducers表里
      val maybeCompletedTxn = updateProducers(batch, updatedProducers, isFromClient = isFromClient)
      maybeCompletedTxn.foreach(completedTxns += _)
    }
    (updatedProducers, completedTxns.toList, None)
  }

  private def updateProducers(batch: RecordBatch,
                              producers: mutable.Map[Long, ProducerAppendInfo],
                              isFromClient: Boolean): Option[CompletedTxn] = {
    val producerId = batch.producerId
    // 获取该 produce 对应的AppendInfo，如果没有则新建一个
    val appendInfo = producers.getOrElseUpdate(producerId, producerStateManager.prepareUpdate(producerId, isFromClient))
    // 将消息batch添加appendInfo里，添加过程中包含了校检序列号
    appendInfo.append(batch)
  }    
}
```



ProducerStateManager保存所有producer最近添加的消息batch。一个ProducerStateManager只负责管理一个分区的producer信息。

```scala
class ProducerStateManager(val topicPartition: TopicPartition,   
                           @volatile var logDir: File,
                           val maxProducerIdExpirationMs: Int = 60 * 60 * 1000) extends Logging {
  // Key为produce_id，Value为对应的信息，由ProducerStateEntry类表示
  private val producers = mutable.Map.empty[Long, ProducerStateEntry]
        
  def lastEntry(producerId: Long): Option[ProducerStateEntry] = producers.get(producerId)

  def prepareUpdate(producerId: Long, isFromClient: Boolean): ProducerAppendInfo = {
    val validationToPerform =
      if (!isFromClient)
        // 如果是Kafka节点之间的请求，那么这里不做任何校检
        ValidationType.None
      else if (topicPartition.topic == Topic.GROUP_METADATA_TOPIC_NAME)
        // 如果是向__consumer_offsets内部topic写数据，则只检验epoch
        ValidationType.EpochOnly
      else
        // 检验 epoch 和 序列号
        ValidationType.Full
    // 如果有该producer的信息，则直接返回。否则调用 empty 方法新建一个
    val currentEntry = lastEntry(producerId).getOrElse(ProducerStateEntry.empty(producerId))
    // 生成ProducerAppendInfo实例
    new ProducerAppendInfo(producerId, currentEntry, validationToPerform)
  }
}    
    
```



ProducerStateEntry类表示了producer的所有信息，它主要包含了最近添加的消息batch。ProducerStateEntry最多只能包含5个消息batch，当有新的消息batch添加进来，会将旧的消息batch删除，来保持队列的长度始终为5。

注意这里只是包含了batch的元数据。消息batch的元数据包含，第一条消息和最后一条消息的序列号。

```scala
private[log] object ProducerStateEntry {
  // 最大保存batch的数目
  private[log] val NumBatchesToRetain = 5
  def empty(producerId: Long) = new ProducerStateEntry(producerId, mutable.Queue[BatchMetadata](), RecordBatch.NO_PRODUCER_EPOCH, -1, None)
}  

private[log] class ProducerStateEntry(val producerId: Long,
                                      val batchMetadata: mutable.Queue[BatchMetadata],  // 消息batch的元数据列表
                                      var producerEpoch: Short,
                                      var coordinatorEpoch: Int,
                                      var currentTxnFirstOffset: Option[Long]) {
    
  // 添加消息batch
  def addBatch(producerEpoch: Short, lastSeq: Int, lastOffset: Long, offsetDelta: Int, timestamp: Long): Unit = {
    // 如果该消息batch的epoch大，则更新producerEpoch
    maybeUpdateEpoch(producerEpoch)
    // 添加消息batch的元数据
    addBatchMetadata(BatchMetadata(lastSeq, lastOffset, offsetDelta, timestamp))
  }
    
  private def addBatchMetadata(batch: BatchMetadata): Unit = {
    // 如果保存的batch数目，达到了5个，则将旧的剔除掉
    if (batchMetadata.size == ProducerStateEntry.NumBatchesToRetain)
      batchMetadata.dequeue()
    // 添加到队列里
    batchMetadata.enqueue(batch)
  }     
    
}    
    
```



注意到上面实例化ProducerAppendInfo对象后，最后调用了它的 append 方法，来校检消息batch的有效性。ProducerAppendInfo最后还会生成新的ProducerStateEntry对象，存储最新一次添加batch的信息，然后替换旧的信息。

ProducerStateEntry每次添加新的消息batch时，都会检查它的epoch 和 sequence，校检步骤如下：

1. 检查消息batch的produce epoch。如果该消息barch的epoch比上条消息小，则会报错。
2. 检查消息batch的sequence，分为两种情况
   1. 如果消息batch的epoch比上条消息大，它的序列号必须从0开始。否则就会出错。
   2. 如果消息batch的epoch相等，它的起始序列号和上条消息batch的结束序列号，必须是连续递增的，否则就会出错。

ProducerAppendInfo会初始化一个新的ProducerStateEntry，新添加的消息batch，都会存到这个新的里面。

```scala
private[log] class ProducerAppendInfo(val producerId: Long,
                                      val currentEntry: ProducerStateEntry,  // 上次添加的消息batch的元数据
                                      val validationType: ValidationType) {  // 校检策略
  // 初始化新的ProducerStateEntry，保存到updatedEntry属性                                     
  private val updatedEntry = ProducerStateEntry.empty(producerId)
  updatedEntry.producerEpoch = currentEntry.producerEpoch
  updatedEntry.coordinatorEpoch = currentEntry.coordinatorEpoch
  updatedEntry.currentTxnFirstOffset = currentEntry.currentTxnFirstOffset    

  private def checkProducerEpoch(producerEpoch: Short): Unit = {
    // 如果该消息batch的produce_epoch比之前的还要下，那么就抛出ProducerFencedException错误
    if (producerEpoch < updatedEntry.producerEpoch) {
      throw new ProducerFencedException(s"Producer's epoch is no longer valid. There is probably another producer " +
        s"with a newer epoch. $producerEpoch (request epoch), ${updatedEntry.producerEpoch} (server epoch)")
    }
  }    
    
  private def checkSequence(producerEpoch: Short, appendFirstSeq: Int): Unit = {
    // 因为之前已经检查过了produce_epoch，如果出现了不相等的情况，只能是该消息batch的produce_epoch大
    if (producerEpoch != updatedEntry.producerEpoch) {
      // 如果是新的produce_epoch，那么它发送过来的第一个消息batch的序列号只能从0开始
      if (appendFirstSeq != 0) {
        // 如果是旧的produce，那么就抛出OutOfOrderSequenceException异常，表示此消息发送的序列号有问题
        // 否则抛出UnknownProducerIdException异常
        if (updatedEntry.producerEpoch != RecordBatch.NO_PRODUCER_EPOCH) {
          throw new OutOfOrderSequenceException(s"Invalid sequence number for new epoch: $producerEpoch " +
            s"(request epoch), $appendFirstSeq (seq. number)")
        } else {
          throw new UnknownProducerIdException(s"Found no record of producerId=$producerId on the broker. It is possible " +
            s"that the last message with the producerId=$producerId has been removed due to hitting the retention limit.")
        }
      }
    } else {
      // 获取最后一次添加的消息batch的结束序列号
      val currentLastSeq = if (!updatedEntry.isEmpty)
        // updatedEntry不为空，那么表示上个消息batch存在于updatedEntry
        updatedEntry.lastSeq
      else if (producerEpoch == currentEntry.producerEpoch)
        // updatedEntry为空，那么表示上个消息batch存在于currentEntry
        currentEntry.lastSeq
      else
        // 如果是新建的producer，那么它的序列号为NO_SEQUENCE
        RecordBatch.NO_SEQUENCE
      
      if (currentLastSeq == RecordBatch.NO_SEQUENCE && appendFirstSeq != 0) {
        // 如果是新建的producer，那么它的第一条消息batch的序列号必须为0，否则抛出OutOfOrderSequenceException异常
        throw new OutOfOrderSequenceException(s"Out of order sequence number for producerId $producerId: found $appendFirstSeq " +
          s"(incoming seq. number), but expected 0")
      } else if (!inSequence(currentLastSeq, appendFirstSeq)) {
        // 继续检查序列号是否连续，否则抛出OutOfOrderSequenceException异常
        throw new OutOfOrderSequenceException(s"Out of order sequence number for producerId $producerId: $appendFirstSeq " +
          s"(incoming seq. number), $currentLastSeq (current end sequence number)")
      }
    }
  }

  // 检查序列号的连续性
  private def inSequence(lastSeq: Int, nextSeq: Int): Boolean = {
    // 这里需要注意下，Int.MaxValue 的下个连续值等于 0
    nextSeq == lastSeq + 1L || (nextSeq == 0 && lastSeq == Int.MaxValue)
  }    
    
}
```

​                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  

当消息成功存储到文件中后，Kafka会根据此次的AppendInfo，生成新的ProducerStateEntry。

```scala
private[log] class ProducerAppendInfo(...) {
  // 新添加的消息batch都会保存到 updatedEntry 
  private val updatedEntry = ProducerStateEntry.empty(producerId)    
    
  // 这里生成新的ProducerStateEntry，是直接返回updatedEntry                                      
  def toEntry: ProducerStateEntry = updatedEntry  
}

class ProducerStateManager(val topicPartition: TopicPartition,
                           @volatile var logDir: File,
                           val maxProducerIdExpirationMs: Int = 60 * 60 * 1000) extends Logging {
  // Key为produce_id，Value为最近添加的信息
  private val producers = mutable.Map.empty[Long, ProducerStateEntry]  
    
  // 负责替换掉旧的ProducerStateEntry
  def update(appendInfo: ProducerAppendInfo): Unit = {
    if (appendInfo.producerId == RecordBatch.NO_PRODUCER_ID)
      throw new IllegalArgumentException(s"Invalid producer id ${appendInfo.producerId} passed to update " +
        s"for partition $topicPartition")

    trace(s"Updated producer ${appendInfo.producerId} state to $appendInfo")
    // 根据此次添加的结果，生成新的ProducerStateEntry
    val updatedEntry = appendInfo.toEntry
    // 保存到producers表中
    producers.get(appendInfo.producerId) match {
      case Some(currentEntry) =>
        currentEntry.update(updatedEntry)

      case None =>
        producers.put(appendInfo.producerId, updatedEntry)
    }
    ......
  }
}
```





## 错误处理

当Producer收到出错的响应时，首先会去检测该错误是否重试。如果支持重试，会将这条失败的请求重新添加到队列里，等待再次发送。否则就会修改序列号，或者重置 produce id。

```java
public class Sender implements Runnable {

    private void completeBatch(ProducerBatch batch, ProduceResponse.PartitionResponse response, long correlationId, long now, long throttleUntilTimeMs) {
        // 检测错误
        Errors error = response.error;
        if (error == Errors.MESSAGE_TOO_LARGE && batch.recordCount > 1 &&
                (batch.magic() >= RecordBatch.MAGIC_VALUE_V2 || batch.isCompressed())) {
            // 这里处理MESSAGE_TOO_LARGE异常，如果该消息batch有多条数据，这里会将消息batch切分成小的batch，再次发送 
            ......
        } else if (error != Errors.NONE) {
            // 检查发生这个错误，是否支持重试
            if (canRetry(batch, response)) {
                if (transactionManager == null) {
                    // 重新添加到发送队列里
                    reenqueueBatch(batch, now);
                } else if (transactionManager.hasProducerIdAndEpoch(batch.producerId(), batch.producerEpoch())) {
                    // 重新添加到发送队列里
                    reenqueueBatch(batch, now);
                } else {
                    // 认为
                    failBatch(batch, response, new OutOfOrderSequenceException("..."), false);
                }
            } else if (error == Errors.DUPLICATE_SEQUENCE_NUMBER) {
                // 如果是重复，说明该消息batch是重复的，之前就添加到了Kafka。这里就直接认为添加成功了
                completeBatch(batch, response);
            } else {
                final RuntimeException exception;
                .... // 初始化错误
                // 处理响应的错误
                failBatch(batch, response, exception, batch.attempts() < this.retries);
            }
        }
        
     private void reenqueueBatch(ProducerBatch batch, long currentTimeMs) {
        // 重新添加到RecordAccumulator
        this.accumulator.reenqueue(batch, currentTimeMs);
 }
```

 

### 重新添加到队列

错误都是可重试的，会将消息重新插入到队列里。注意插入消息，是需要保证队列的序列号的顺序。RecordAccumulator提供了reenqueue方法，支持重新插入。

```java
public final class RecordAccumulator {

    public void reenqueue(ProducerBatch batch, long now) {
        batch.reenqueued(now);
        Deque<ProducerBatch> deque = getOrCreateDeque(batch.topicPartition);
        synchronized (deque) {
            if (transactionManager != null)
                // 开启了幂等性，需要按照序列号大小，添加到队列里
                insertInSequenceOrder(deque, batch);
            else
                deque.addFirst(batch);
        }
    }
    
    private void insertInSequenceOrder(Deque<ProducerBatch> deque, ProducerBatch batch) {
        ProducerBatch firstBatchInQueue = deque.peekFirst();
        if (firstBatchInQueue != null && firstBatchInQueue.hasSequence() && firstBatchInQueue.baseSequence() < batch.baseSequence()) {
            // 注意这里会检查重新添加的消息batch的序列号，需要比最后一条消息batch要小
            List<ProducerBatch> orderedBatches = new ArrayList<>();
            // 将序列号小的消息batch，先提取出来，添加到新队列orderedBatches里
            while (deque.peekFirst() != null && deque.peekFirst().hasSequence() && deque.peekFirst().baseSequence() < batch.baseSequence())
                orderedBatches.add(deque.pollFirst());
            // 添加该消息batch到队列中
            deque.addFirst(batch);

            // 就序列号小的消息batch再添加到队列中
            for (int i = orderedBatches.size() - 1; i >= 0; --i) {
                deque.addFirst(orderedBatches.get(i));
            }

            // 添加完后，原有的队列就是有序的
        } else {
            deque.addFirst(batch);
        }    
}
```



### 重置  produce id

如果尝试多次，仍然遇到OutOfOrderSequenceException异常，Kafka会认为无法找到缺失的那条记录，而无法修复。所以Producer为了不影响之后的消息发送，它会重置 produce id，然后申请新的 produce id，继续发送请求。

```java
public class Sender implements Runnable {

    private void failBatch(ProducerBatch batch, long baseOffset, long logAppendTime, RuntimeException exception, boolean adjustSequenceNumbers) {
        if (transactionManager != null) {
            if (exception instanceof OutOfOrderSequenceException
                    && !transactionManager.isTransactional()  // isTransactional方法返回false，表示这里仅仅开启了幂等性，并没有开启事务
                    && transactionManager.hasProducerId(batch.producerId())) {  // 是否之前就已经申请了produce_id
                // 重置produce_id为空，等待请求新的produce_id
                transactionManager.resetProducerId();
            } else if (exception instanceof ClusterAuthorizationException
                    || exception instanceof TransactionalIdAuthorizationException
                    || exception instanceof ProducerFencedException
                    || exception instanceof UnsupportedVersionException) {
                // 这些异常是不可恢复的，所以这里将transactionManager的状态设置为错误
                transactionManager.transitionToFatalError(exception);
            } else if (transactionManager.isTransactional()) {
                transactionManager.transitionToAbortableError(exception);
            }
            transactionManager.removeInFlightBatch(batch);
            if (adjustSequenceNumbers)
                // 如果消息batch即使切分还是因为消息长度过大，那么会跳过这个消息batch，并且将更改之后的序列号
                transactionManager.adjustSequencesDueToFailedBatch(batch);
        }
        // 执行batch的回调函数
        if (batch.done(baseOffset, logAppendTime, exception))
            // 释放batch
            this.accumulator.deallocate(batch);
    }
}
```



### 修改序列号

当消息batch多次切分，还是因为消息长度过大，那么Kafka会认为这个batch失败，然后会跳过这个batch，更改之后的序列号。

```java
public class TransactionManager {
    
    // 存储着分区的下一个消息的序列号
    private final Map<TopicPartition, Integer> nextSequence;   
    
    // 存储着正在发送的消息batch
    private final Map<TopicPartition, PriorityQueue<ProducerBatch>> inflightBatchesBySequence;    

    synchronized void adjustSequencesDueToFailedBatch(ProducerBatch batch) {
        if (!this.nextSequence.containsKey(batch.topicPartition))
            return;
     
        int currentSequence = sequenceNumber(batch.topicPartition);
        currentSequence -= batch.recordCount;
        if (currentSequence < 0)
            throw new IllegalStateException("Sequence number for partition " + batch.topicPartition + " is going to become negative : " + currentSequence);
        // 更新该分区的下个序列号，这样之后的消息就会从这个序列号开始，填补了这个序列号的缺失
        setNextSequence(batch.topicPartition, currentSequence);

        for (ProducerBatch inFlightBatch : inflightBatchesBySequence.get(batch.topicPartition)) {
            if (inFlightBatch.baseSequence() < batch.baseSequence())
                continue;
            // 如果有在这条消息batch之后，发送的消息，需要更新它的序列号
            // 序列号变为减去batch的消息数目
            int newSequence = inFlightBatch.baseSequence() - batch.recordCount;
            if (newSequence < 0)
                throw new IllegalStateException("....");
            // 更新新的序列号
            inFlightBatch.resetProducerState(new ProducerIdAndEpoch(inFlightBatch.producerId(), inFlightBatch.producerEpoch()), newSequence, inFlightBatch.isTransactional());
        }
    }
}
```

