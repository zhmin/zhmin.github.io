---
title: Datax Channel原理
date: 2018-12-16 21:49:23
tags: datax, channel
---

# Channel 原理 #

Channel是Reader和Writer的通信组件。Reader向channle写入数据，Writer从channel读取数据。channel还提供了限速的功能，支持数据大小（字节数）， 数据条数。

## 写入数据 ##

Channel提供push方法，给Reader调用，写入数据。
```java
    public void push(final Record r) {
        Validate.notNull(r, "record不能为空.");
        // 子类实现doPush方法
        this.doPush(r);
        // statPush会进行限速
        this.statPush(1L, r.getByteSize());
    }
    
    public void pushAll(final Collection<Record> rs) {
        Validate.notNull(rs);
        Validate.noNullElements(rs);
        // 子类实现doPush方法
        this.doPushAll(rs);
        // statPush会进行限速
        this.statPush(rs.size(), this.getByteSize(rs));
    }

```
statPush里面会对速度进行控制。它通过Communication记录总的写入数据大小和数据条数。然后每隔一段时间，检查速度。如果速度过快，就会sleep一段时间，来把速度降下来。

CommunicationTool 提供方法，从Communication计算读取的字节数，条数
```java
public final class CommunicationTool {
    public static long getTotalReadRecords(final Communication communication) {
        return communication.getLongCounter(READ_SUCCEED_RECORDS) +
                communication.getLongCounter(READ_FAILED_RECORDS);
    }

    public static long getTotalReadBytes(final Communication communication) {
        return communication.getLongCounter(READ_SUCCEED_BYTES) +
                communication.getLongCounter(READ_FAILED_BYTES);
    }
}
```

```java
    private void statPush(long recordSize, long byteSize) {
        // currentCommunication实时记录了Reader读取的总数据字节数和条数
        currentCommunication.increaseCounter(CommunicationTool.READ_SUCCEED_RECORDS,
                recordSize);
        currentCommunication.increaseCounter(CommunicationTool.READ_SUCCEED_BYTES,
                byteSize);
        ......

        // 判断是否会限速
        boolean isChannelByteSpeedLimit = (this.byteSpeed > 0);
        boolean isChannelRecordSpeedLimit = (this.recordSpeed > 0);
        if (!isChannelByteSpeedLimit && !isChannelRecordSpeedLimit) {
            return;
        }
        // lastCommunication记录最后一次的时间
        long lastTimestamp = lastCommunication.getTimestamp();
        long nowTimestamp = System.currentTimeMillis();
        long interval = nowTimestamp - lastTimestamp;
        // 每隔flowControlInterval一段时间，就会检查是否超速
        if (interval - this.flowControlInterval >= 0) {
            long byteLimitSleepTime = 0;
            long recordLimitSleepTime = 0;
            if (isChannelByteSpeedLimit) {
                // 计算速度，(现在的字节数 - 上一次的字节数) / 过去的时间
                long currentByteSpeed = (CommunicationTool.getTotalReadBytes(currentCommunication) -
                        CommunicationTool.getTotalReadBytes(lastCommunication)) * 1000 / interval;
                if (currentByteSpeed > this.byteSpeed) {
                    // 计算根据byteLimit得到的休眠时间，
                    // 这段时间传输的字节数 / 期望的限定速度 - 这段时间
                    byteLimitSleepTime = currentByteSpeed * interval / this.byteSpeed
                            - interval;
                }
            }

            if (isChannelRecordSpeedLimit) {
                long currentRecordSpeed = (CommunicationTool.getTotalReadRecords(currentCommunication) -
                        CommunicationTool.getTotalReadRecords(lastCommunication)) * 1000 / interval;
                if (currentRecordSpeed > this.recordSpeed) {
                    // 计算根据recordLimit得到的休眠时间
                    recordLimitSleepTime = currentRecordSpeed * interval / this.recordSpeed
                            - interval;
                }
            }

            // 休眠时间取较大值
            long sleepTime = byteLimitSleepTime < recordLimitSleepTime ?
                    recordLimitSleepTime : byteLimitSleepTime;
            if (sleepTime > 0) {
                try {
                    Thread.sleep(sleepTime);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }

            // 保存读取字节数
            lastCommunication.setLongCounter(CommunicationTool.READ_SUCCEED_BYTES,
                    currentCommunication.getLongCounter(CommunicationTool.READ_SUCCEED_BYTES));
            // 保存读取失败的字节数
            lastCommunication.setLongCounter(CommunicationTool.READ_FAILED_BYTES,
                    currentCommunication.getLongCounter(CommunicationTool.READ_FAILED_BYTES));
            // 保存读取条数
            lastCommunication.setLongCounter(CommunicationTool.READ_SUCCEED_RECORDS,
                    currentCommunication.getLongCounter(CommunicationTool.READ_SUCCEED_RECORDS));
            // 保存读取失败的条数
            lastCommunication.setLongCounter(CommunicationTool.READ_FAILED_RECORDS,
                    currentCommunication.getLongCounter(CommunicationTool.READ_FAILED_RECORDS));
            // 记录保存的时间点
            lastCommunication.setTimestamp(nowTimestamp);
        }
    }
```

## 读取数据 ##

Channel提供pull方法，给Writer调用，读取数据。

```java
    public Record pull() {
        // 子类实现doPull方法，返回数据
        Record record = this.doPull();
        // 调用statPull方法，更新统计数据
        this.statPull(1L, record.getByteSize());
        return record;
    }

    public void pullAll(final Collection<Record> rs) {
        Validate.notNull(rs);
        // 子类实现doPullAll方法，返回数据
        this.doPullAll(rs);
        // 调用statPull方法，更新统计数据
        this.statPull(rs.size(), this.getByteSize(rs));
    }
```
statPull方法，并没有限速。因为数据的整个流程是Reader -》 Channle -》 Writer， Reader的push速度限制了，Writer的pull速度也就没必要限速
```java
    private void statPull(long recordSize, long byteSize) {
        currentCommunication.increaseCounter(
                CommunicationTool.WRITE_RECEIVED_RECORDS, recordSize);
        currentCommunication.increaseCounter(
                CommunicationTool.WRITE_RECEIVED_BYTES, byteSize);
    }
```

## MemoryChannel 原理 ##

目前Channel的子类只有MemoryChannel。MemoryChannel实现了doPush和doPull方法。它本质是将数据放进ArrayBlockingQueue。

先看看MemoryChannel的一些属性
```java
public class MemoryChannel extends Channel {
    
    // 等待Reader处理完的时间，也就是pull的时间，继承自Channel
    protected volatile long waitReaderTime = 0;
    // 等待Writer处理完的时间，也就是push的时间，继承自Channel
    protected volatile long waitWriterTime = 0;

    // Channel里面保存的数据大小
	private AtomicInteger memoryBytes = new AtomicInteger(0);
    // 存放记录的queue
	private ArrayBlockingQueue<Record> queue = null;
}
```

### 读写单条数据 ###

首先看push和pull单条的情况

```java
	protected void doPush(Record r) {
		try {
			long startTime = System.nanoTime();
            // ArrayBlockingQueue提供了阻塞的put方法，写入数据
			this.queue.put(r);
            // 记录写入push花费的时间
			waitWriterTime += System.nanoTime() - startTime;
            // 更新Channle里数据的字节数
            memoryBytes.addAndGet(r.getMemorySize());
		} catch (InterruptedException ex) {
			Thread.currentThread().interrupt();
		}
	}

	protected Record doPull() {
		try {
			long startTime = System.nanoTime();
            // ArrayBlockingQueue提供了阻塞的take方法，读取入数据
			Record r = this.queue.take();
            // 记录写入pull花费的时间
			waitReaderTime += System.nanoTime() - startTime;
            // 更新Channle里数据的字节数
			memoryBytes.addAndGet(-r.getMemorySize());
			return r;
		} catch (InterruptedException e) {
			Thread.currentThread().interrupt();
			throw new IllegalStateException(e);
		}
	}

```

### 读写多条数据 ###

再看看push和pull多条的情况，这种方法比起单条的效率更高。 因为ArrayBlockingQueue没有提供批量操作的阻塞方法，所以需要自己使用条件锁。

先看看相关的一些属性
```java
public class MemoryChannel extends Channel {
    // 递归锁
    private ReentrantLock lock;
    // 条件信号
	private Condition notInsufficient, notEmpty;
    // 一次从Channel的pull的数据条数
	private int bufferSize = 0;
    // 数据的字节数容量
    protected int byteCapacity;

	public MemoryChannel(final Configuration configuration) {
        .......
		this.bufferSize = configuration.getInt(CoreConstant.DATAX_CORE_TRANSPORT_EXCHANGER_BUFFERSIZE);
        // 初始化锁
		lock = new ReentrantLock();
		notInsufficient = lock.newCondition();
		notEmpty = lock.newCondition();
	}

```

```java
	protected void doPullAll(Collection<Record> rs) {
		assert rs != null;
		rs.clear();
		try {
			long startTime = System.nanoTime();
            // 获取锁
			lock.lockInterruptibly();
            // 从queue里面取出数据，最多bufferSize条
			while (this.queue.drainTo(rs, bufferSize) <= 0) {
                // 如果queue里面没有数据，就等待notEmpty信号
				notEmpty.await(200L, TimeUnit.MILLISECONDS);
			}
            // 更新pull的时间
			waitReaderTime += System.nanoTime() - startTime;
			int bytes = getRecordBytes(rs);
            // 更新数据的字节数
			memoryBytes.addAndGet(-bytes);
            // 通知可以push数据的信号
			notInsufficient.signalAll();
		} catch (InterruptedException e) {
			throw DataXException.asDataXException(
					FrameworkErrorCode.RUNTIME_ERROR, e);
		} finally {
			lock.unlock();
		}
	}

    protected void doPushAll(Collection<Record> rs) {
		try {
			long startTime = System.nanoTime();
            // 获取锁
			lock.lockInterruptibly();
			int bytes = getRecordBytes(rs);
            
			while (memoryBytes.get() + bytes > this.byteCapacity || rs.size() > this.queue.remainingCapacity()) {
                // 如果新增数据，会造成数据字节数超过指定容量， 或者超过了queue的容量，就会一直等待notInsufficient信号
				notInsufficient.await(200L, TimeUnit.MILLISECONDS);
            }
            // 向queue里添加数据
			this.queue.addAll(rs);
            // 更新push的时间
			waitWriterTime += System.nanoTime() - startTime;
            // 更新数据的字节数
			memoryBytes.addAndGet(bytes);
            // 通知可以pull数据的信号
			notEmpty.signalAll();
		} catch (InterruptedException e) {
			throw DataXException.asDataXException(
					FrameworkErrorCode.RUNTIME_ERROR, e);
		} finally {
			lock.unlock();
		}
	}
```