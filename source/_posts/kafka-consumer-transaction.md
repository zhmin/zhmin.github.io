---
title: Kafka Consumer 读取事务消息
date: 2019-04-20 22:05:15
tags: kafka, consumer, transaction
categories: kafka
---

# Kafka Consumer 读取事务消息



Kafka 在每次发送事务消息之后，还需要发送确认消息，才能表示此次事务完成。确认消息可以是事务确认成功的消息，也可以是事务终止的消息。如果是事务终止，那么此次事务需要回滚，所有涉及到该事务之前的消息都应该废弃。Kafka Consumer在读取这些消息时，需要结合事务状态，来滤掉这些废弃的消息。所以Kafka服务端返回消息时，也会附带祥光的事务信息。



## 事务索引文件

事务索引文件保存了所有的终止事务的信息，这些信息包含事务的起始和结束位置等。

- 起始位置，该事务的第一条消息的位置
- 结束位置，该事务的最后一条消息的位置
- Last Stable Offset，表示在该位置前面，所有的数据都已经是确认好的，没有正在执行的事务

当Consumer请求消息时，Kafka服务端不仅会返回消息，还会返回对应范围内的所有Aborted 事务消息。查找事务的代码如下：

```scala
class TransactionIndex(val startOffset: Long, @volatile var file: File) extends Logging {

  // 找到指定范围内的消息，和它有交集的事务，
  // fetchOffset为起始位置，upperBoundOffset表示结束位置
  def collectAbortedTxns(fetchOffset: Long, upperBoundOffset: Long): TxnIndexSearchResult = {
    val abortedTransactions = ListBuffer.empty[AbortedTxn]
    // 遍历事务的Aborted消息
    for ((abortedTxn, _) <- iterator()) {
      // 下面这个if条件，判断是否和这个事务有交集
      if (abortedTxn.lastOffset >= fetchOffset && abortedTxn.firstOffset < upperBoundOffset)
        abortedTransactions += abortedTxn
      // 这个if条件，判断是否结束遍历事务。如果事务的lastStableOffset必须大于结束位置，表示该事务已经和这个范围的消息没有交集了
      if (abortedTxn.lastStableOffset >= upperBoundOffset)
        return TxnIndexSearchResult(abortedTransactions.toList, isComplete = true)
    }
    TxnIndexSearchResult(abortedTransactions.toList, isComplete = false)
  }
}
```





## Consumer 过滤事务消息

当Consumer收到响应后，会结合Aborted 事务消息，过滤掉因为事务没有成功的消息。过滤原理如下图所示，下面表示服务端返回的响应，有多个消息batch，其中涉及到两个事务，A 和 B。 还有每个事务的起始位置：

<img src="kafka-consumer-transcation.svg">

当Consumer遍历到batch 0时，发现它属于事务A的消息，通过比较事务A的起始和结束位置（Aborted消息的位置），可以判断出该batch是在终止事务A的，所以会跳过。

同理当Consumer遍历到batch 1时，发现它属于事务B的消息，并且是在终止的事务中，所以也会跳过。

当Consumer遍历到 batch 2，它是事务A的终止消息batch。这个batch很特殊，它不包含任何数据，只是表示事务的终止，所以它也会跳过。

当Consumer遍历到 batch 3，发现它并不在任何终止事务中，所以认为这个batch是合法的，会返回。

解析来的遍历原理同上，最后的返回结果，只包含了batch 3 和 batch 5。



### 源码解析

首先创建一个优先队列，存储Aborted 事务消息，排序依照事务的起始offset。

然后遍历消息batch，根据消息batch的 末尾位置，找到所有可能与它相关的Aborted 事务消息，将这些涉及到的produce id 保存起来。根据这些produce id，就可以判断出此条消息是否为废弃的事务消息。

如果此条消息是Aborted 事务消息，那么说明对应的produce id的事务已经确定了，就将 produce id 从集合中删除掉。

```java
private class PartitionRecords {
    // 消息batch列表
    private final Iterator<? extends RecordBatch> batches;
    // 保存了那些producer，发送的消息为事务终止
    private final Set<Long> abortedProducerIds;
    // record结果列表，从batch中生成
    private CloseableIterator<Record> records;
    // 保存了所有了终止事务的信息
    private final PriorityQueue<FetchResponse.AbortedTransaction> abortedTransactions;
    
    private Record nextFetchedRecord() {
        while (true) {
            if (records == null || !records.hasNext()) {
                ....
                // 如果records遍历完了，需要从下个batch生成
                currentBatch = batches.next();
                // 注意到isolationLevel，它可以设置只读取事务成功的消息，这样就可以过滤掉由于事务终止的废弃消息
                if (isolationLevel == IsolationLevel.READ_COMMITTED && currentBatch.hasProducerId()) {
                    // 更新abortedProducerIds列表
                    consumeAbortedTransactionsUpTo(currentBatch.lastOffset());

                    long producerId = currentBatch.producerId();
                    if (containsAbortMarker(currentBatch)) {
                        // 如果是终止事务batch，那么就从abortedProducerIds列表中，将对应的produce id删除，
                        // 因为该消息表示该事务的终止，表示该producer之后发送的消息，已经不再属于上次终止的事务了。
                        abortedProducerIds.remove(producerId);
                    } else if (isBatchAborted(currentBatch)) {
                        // 如果确定该batch因为事务终止而废弃的，那么跳过
                        nextFetchOffset = currentBatch.nextOffset();
                        continue;
                    }
                }
                // 从batch中生成record列表
                records = currentBatch.streamingIterator(decompressionBufferSupplier);
            } else {
                // 遍历batch里的record
                Record record = records.next();
                    if (record.offset() >= nextFetchOffset) {
                        maybeEnsureValid(record);
                        // 这里如果遇到事务确认成功的消息batch，则需要跳过
                        if (!currentBatch.isControlBatch()) {
                            return record;
                        } else {
                            // 通过设置nextFetchOffset，跳过这个batch（因为这个batch是只包含一个事务成功的取人消息，）
                            nextFetchOffset = record.offset() + 1;
                        }
                    }
                }
            }
        }
    }
     
    private boolean isBatchAborted(RecordBatch batch) {
        // 如果该batch是事务类型，并且它的produce id 在abortedProducerIds集合里
        return batch.isTransactional() && abortedProducerIds.contains(batch.producerId());
    }
    
    private void consumeAbortedTransactionsUpTo(long offset) {
        if (abortedTransactions == null)
            return;
        // 找到那些事务，它的跨度包含了当前batch
        while (!abortedTransactions.isEmpty() && abortedTransactions.peek().firstOffset <= offset) {           // 这里会从abortedTransactions提取事务信息
            FetchResponse.AbortedTransaction abortedTransaction = abortedTransactions.poll();
            // 并且将它的produce id 添加到abortedProducerIds集合里，表示现在produce id发送的消息处于终止事务里
            abortedProducerIds.add(abortedTransaction.producerId);
        }
    }   
}
```





## 参考资料

 <https://www.confluent.io/blog/transactions-apache-kafka/>

<https://docs.google.com/document/d/11Jqy_GjUGtdXJK94XGsEIK7CP1SnQGdp2eF0wSw9ra8/edit#>

<https://docs.google.com/document/d/1Rlqizmk7QCDe8qAnVW5e5X8rGvn6m2DCR3JR2yqwVjc/edit>