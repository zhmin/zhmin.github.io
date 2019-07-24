---
title: Kafka 客户端的缓存管理
date: 2019-07-24 20:49:00
tags: kafka, buffer
categories: kafka
---

## 前言

Kafka 作为一个高吞吐量的消息队列，它的很多设计都体现了这一点。比如它的客户端，无论是 Producer 还是 Consumer    ，都会内置一个缓存用来存储消息。这样类似于我们写文件时，并不会一次只写一个字节，而是先写到一个缓存里，然后等缓存满了，才会将缓存里的数据写入到磁盘。这种缓存机制可以有效的提高吞吐量，本篇文章介绍缓存在 Kafka 客户端的实现原理。



## Producer 缓存

我们知道 Producer 发送消息，会先将它存到 RecordAccumulator 的缓存里，等待缓存满了之后，就会发送到服务端。这个缓存的大小，是由内部的内存池控制的。



### 内存池使用

我们通过观察 RecordAccumulator 的 append 接口，可以看到每次缓存消息之前，都会向内存池申请内存。

```java
public final class RecordAccumulator {
    // 消息缓存队列，以batch格式存储
    private final ConcurrentMap<TopicPartition, Deque<ProducerBatch>> batches;    
    // 内存池
    private final BufferPool free;
    
    public RecordAppendResult append(....) {
        // 计算该消息占用的内存大小    
        int size = Math.max(this.batchSize, AbstractRecords.estimateSizeInBytesUpperBound(maxUsableMagic, compression, key, value, headers));
        // 向内存池申请内存
        buffer = free.allocate(size, maxTimeToBlock);
        .......
    }
}
```



当消息发送后，会触发 RecordAccumulator 释放内存。

```java
public final class RecordAccumulator {
    // 内存池
    private final BufferPool free;
    
    public void deallocate(ProducerBatch batch) {
        incomplete.remove(batch);
        // 检测是否该消息batch数据太大了，如果太大了则需要切割。所以这种情况不需要释放内存
        if (!batch.isSplitBatch())
            // 向内存池释放内存
            free.deallocate(batch.buffer(), batch.initialCapacity());
    }    
}
```





### 内存池结构

使用内存池有两个优点，一个是能够限制内存的使用量，另一个是减少内存的申请和回收频率。虽然 java 支持自动 gc ，但是 gc 也是有成本的。如果之前申请的内存用完之后，还可以重新复用，那么就不会触发 gc。但是内存池的实现有一个难点，那就是如何高效的重新利用。因为每次申请的内存大小都不相同，这样就没办法直接利用了。一种常见的做法是只缓存那些特定大小的内存，对于其他大小的内存则使用后直接丢弃。

我们知道 Kafka 为了提高吞吐量，都是以 batch 格式保存消息。Producer 在实现内存池时，它结合了消息 batch 的特点，试图将每个消息 batch 的大小控制在一定范围内。这样每次申请内存的大小，就可以是相同的。基于这个原因，Kafka 的内存池分为两部分。一部分是特定大小内存块的缓存池，另一个是非缓存池。

<img src="kafka-producer-bufferpool.svg">



当申请的内存大小等于特定的数值，则优先从缓存池中获取。如果缓存池没有，那么需要向非缓存池部分申请内存。等到这块内存使用完后，才会被放入到缓存池等待复用。注意到缓存池的大小是可变的，一开始为零。随着用户申请和释放，才慢慢增长起来的。

如果申请的内存不等于特定的数值，则向非缓存池申请。如果内存空间不够用，那么就需要释放缓存池的内存。

缓存池的内存一般都很少回收，除非是内存空间不足。而非缓存池的内存，都是使用后丢弃，等待 gc 回收。



### 内存池实现

BufferPool 类负责实现内存池，它有两个重要接口：

- allocate 接口，负责申请内存
- deallocate 接口，负责释放内存

allocate 接口代码简化如下，它支持用户并发申请内存，里面包含了一个等待的用户队列，队列采用了先进先出的方式。

```java
public class BufferPool {
    private final long totalMemory;  // 整个内存池的总容量
    private long nonPooledAvailableMemory;  // 非缓存池的空闲大小    
    private final int poolableSize;  // 缓存池中，特定内存的大小
    private final ReentrantLock lock;  // 锁用来防治并发
    private final Deque<ByteBuffer> free;  // 缓存池中的空闲内存块
    private final Deque<Condition> waiters;  // 等待申请的用户

    // 参数size表示申请的内存大小，参数maxTimeToBlockMs表示等待的最长时间
    public ByteBuffer allocate(int size, long maxTimeToBlockMs) throws InterruptedException {
        if (size > this.totalMemory)
            throw new IllegalArgumentException("...")
        ByteBuffer buffer = null;
        this.lock.lock();
        // 如果申请大小等于指定的值，并且缓存池中有空闲的内存块，则直接返回
         if (size == poolableSize && !this.free.isEmpty())
             return this.free.pollFirst();
        // 计算总的空闲内存大小， 等于缓存池的大小 + 非缓存池的空闲大小
        int freeListSize = freeSize() * this.poolableSize;
        if (this.nonPooledAvailableMemory + freeListSize >= size) {
            // 如果空闲内存足够，那么需要保证非缓存池的空闲空间足够
            // 因为所有的内存分配都是从非缓存池开始
            freeUp(size);
            this.nonPooledAvailableMemory -= size;
        } else {
            // 如果空闲内存不够，那么需要等待别的用户释放内存
            int accumulated = 0; // 表示已经成功申请的内存大小
            Condition moreMemory = this.lock.newCondition();
            
            long remainingTimeToBlockNs = TimeUnit.MILLISECONDS.toNanos(maxTimeToBlockMs);
            // 添加到等待集合中
            this.waiters.addLast(moreMemory);
            // 循环等待
            while (accumulated < size) {
                ......
                // 等待通知
                waitingTimeElapsed = !moreMemory.await(remainingTimeToBlockNs, TimeUnit.NANOSECONDS);
                if (waitingTimeElapsed) {
                    // 超过等待时间还没有分配到足够的内存，那么就抛出异常
                    throw new TimeoutException("Failed to allocate memory within the configured max blocking time " + maxTimeToBlockMs + " ms.");
                }
                // 如果申请大小等于指定的值，而这时刚好有其他用户释放了内存，那么就直接从缓存池中获取
                if (accumulated == 0 && size == this.poolableSize && !this.free.isEmpty()) {
                    buffer = this.free.pollFirst();
                    accumulated = size;
                } else {
                    // 继续释放足够的内存
                    freeUp(size - accumulated);
                    // 计算可以分配的内存大小
                    int got = (int) Math.min(size - accumulated, this.nonPooledAvailableMemory);
                    // 更改非缓存池的空闲内存大小
                    this.nonPooledAvailableMemory -= got;
                    accumulated += got;
                }
            }

            // 这里会检查是否还有剩下的空闲内存，如果有则需要通知下一个用户。
            // 因为这时可能有多个用户都释放了内存，而用户释放内存只会通知第一个用户（也就是当前用户），而下个用户还在一直等待中，如果当前用户不主动通知的话，可能造成下个用户等待超时。
            if (!(this.nonPooledAvailableMemory == 0 && this.free.isEmpty()) && !this.waiters.isEmpty())
                    this.waiters.peekFirst().signal();
            
            // 运行到这里，说明已经成功了申请到了内存          
            if (buffer == null)
                // 表示buffer不是从缓存池中获取的，需要执行内存分配
                return safeAllocateByteBuffer(size);
            else:
                // 已经从缓存池中获取到了，则直接返回
                return buffer;
        }
    }
}   
```



deallocate 接口的源码比较简单

```java
public class BufferPool {
    
    public void deallocate(ByteBuffer buffer) {
        deallocate(buffer, buffer.capacity());
    }

    public void deallocate(ByteBuffer buffer, int size) {
        lock.lock();
        try {
            // 如果释放的内存大小等于指定的值，那么就将它添加到缓存池。free列表存储这些内存块
            if (size == this.poolableSize && size == buffer.capacity()) {
                buffer.clear();
                this.free.add(buffer);
            } else {
                // 否则更新非缓存池的空闲大小，这个ByteBuffer实例等待jvm自动gc
                this.nonPooledAvailableMemory += size;
            }
            // 通知队列的第一个用户
            Condition moreMem = this.waiters.peekFirst();
            if (moreMem != null)
                moreMem.signal();
        } finally {
            lock.unlock();
        }
    }
}
```



## Consumer 缓存

Consumer 从服务端获取消息，也是以消息 batch 的格式获取，然后存到缓存里。KafkaConsumer 提供了 poll 方法读取消息。它的原理是从缓存里直接获取，如果缓存里没有，才会向服务端发出请求。

Fetcher 使用了一个队列，来缓存从服务端获取的响应。当用户从缓存中读取消息时，会依次从队列里解析响应，返回消息。但是用户一次不能获取过多数量的消息，这个阈值由配置项 max.poll.records 指定，默认为500。

Fetcher 类还负责与服务端的交互。这里主要关注两个接口

- fetchedRecords，负责读取缓存消息
- sendFetches，负责发送请求

KafkaConsumer 读取消息的过程如下

```java
public class KafkaConsumer<K, V> implements Consumer<K, V> {
    
    private final Fetcher<K, V> fetcher; // 消息缓存
    
    // poll 方法做过简化，省略了Metadata和topic subcribe的步骤 
    private ConsumerRecords<K, V> poll(final long timeoutMs, final boolean includeMetadataInTimeout) {
        // 调用 pollForFetches 获取消息
        final Map<TopicPartition, List<ConsumerRecord<K, V>>> records = pollForFetches(remainingTimeAtLeastZero(timeoutMs, elapsedTime));
        if (!records.isEmpty()) {
            // 注意到这里，成功获取之后，还会调用sendFetches方法，发送请求
            // 因为kafka发送请求都是异步的，这里提前发出请求，可以有效的减少下次缓存为空而需要等待请求的时间
            if (fetcher.sendFetches() > 0 || client.hasPendingRequests()) {
                client.pollNoWakeup();
            }
            return this.interceptors.onConsume(new ConsumerRecords<>(records));
        }
    }
    
    private Map<TopicPartition, List<ConsumerRecord<K, V>>> pollForFetches(final long timeoutMs) {
        // 首先从缓存中尝试读取
        final Map<TopicPartition, List<ConsumerRecord<K, V>>> records = fetcher.fetchedRecords();
        if (!records.isEmpty()) {
            return records;
        }

        // 如果缓存为空，则向服务端发出请求
        fetcher.sendFetches();
        // 等待服务端的响应
        client.poll(pollTimeout, startMs, () -> {
            // hasCompletedFetches 表示是否有成功的响应
            return !fetcher.hasCompletedFetches();
        });
        // 从缓存中获取消息
        return fetcher.fetchedRecords();
    }
}
```



我们注意到 Consumer 在每次读取消息之后，都会触发一次发送请求，这样对于提高性能有好处，减少了下一次的请求等待时间。但是这样会存在一个问题，假想我们把  max.poll.records 设置为 1，这样每次从服务端返回的消息数量都比 1 大，那么缓存就会持续的增长，造成 OOM。

其实 Fetcher 每次发送请求，并不是拉取所有分区的消息。它的 fetchablePartitions 方法决定了请求的分区，它会检查分区在缓存中是否有对应的消息，如果有那么就不请求。这样就基本保证了缓存里拥有每个分区的消息。

```java
public class Fetcher<K, V> implements SubscriptionState.Listener, Closeable {
    // CompletedFetch代表着响应，对应着一个分区的消息
    private final ConcurrentLinkedQueue<CompletedFetch> completedFetches;
    
    private List<TopicPartition> fetchablePartitions() {
        // 存储着哪些分区不需要请求
        Set<TopicPartition> exclude = new HashSet<>();  
        // 获取分配的分区
        List<TopicPartition> fetchable = subscriptions.fetchablePartitions();
        // nextInLineRecords 表示正在解析的响应
        if (nextInLineRecords != null && !nextInLineRecords.isFetched) {
            exclude.add(nextInLineRecords.partition);
        }
        // 如果在缓存里，该分区已经有了消息，则不需要请求
        for (CompletedFetch completedFetch : completedFetches) {
            exclude.add(completedFetch.partition);
        }
        // 剔除掉那些不需要请求的分区
        fetchable.removeAll(exclude);
        return fetchable;
    }
}
```



对于请求，不仅有分区的限制，还有每次请求返回的数据大小限制。不然如果一次请求的数据过大，容易造成内存溢出。我们可以观察请求的格式，发现有多个值来限制请求大小。

- max_bytes，表示响应数据的最大长度。可以通过配置 fetch.max.bytes 来指定，默认为 50MB
- min_bytes，表示响应数据的最小长度。可以通过配置 fetch.min.bytes 来指定，默认为 1B

每次请求还包含了多个分区，对于每个分区返回的数据大小，也有限制。通过配置 max.partition.fetch.bytes 来指定，默认为 1MB。这样我们能够粗略的计算出缓存的大小，分配的分区数量 * max.partition.fetch.bytes 。