---
title: Spark 内存管理
date: 2019-01-15 22:50:44
tags: spark, memory
categories: spark
---

# Spark 内存管理

spark自己实现了一套内存的管理，为了精准的控制内存。因为JVM的垃圾回收不可控制，尤其是大内存的回收，会造成程序暂停长时间。spark为了避免这种情况，实现了内存的管理，尤其是针对大内存。对于大内存，spark会使用内存池的方法，避免了垃圾回收的次数，提高了程序的稳定性。目前spark支持堆内内存分配和堆外内存分配。



## 内存分配

spark分配内存是以内存块为单位。内存块包含内存地址和内存大小。

### 内存块

内存地址是由MemoryLocation类表示的。它存储着两部分数据，一个是头部，一个是数据。

```java
public class MemoryLocation {

  @Nullable
  Object obj;       // 内存的起始地址

  long offset;     // 数据的偏移位置
}
```

内存块由MemoryBlock表示，它除了包含了内存地址，还有内存大小。

```java
public class MemoryBlock extends MemoryLocation {
  private final long length;     //  内存中的数据大小
}
```



### 内存分配接口

MemoryAllocator定义了内存分配的接口。HeapMemoryAllocator类实现了MemoryAllocator接口，支持在堆内分配内存。UnsafeMemoryAllocator则支持在堆外分配内存。

### 堆内分配

HeapMemoryAllocator对于大的数据块，会缓存下来，注意这里是弱引用。当大的内存用完后，但是没有被jvm回收之前，会提供给新的需求。这样可以尽量的减少 jvm 垃圾回收。

HeapMemoryAllocator的分配，只是实例化一个Long类型的数组。

```java
public class HeapMemoryAllocator implements MemoryAllocator {
  // 缓存池， Key的内存块的大小， Value为内存块的弱引用
  @GuardedBy("this")
  private final Map<Long, LinkedList<WeakReference<MemoryBlock>>> bufferPoolsBySize =
    new HashMap<>();
  
  @Override
  public MemoryBlock allocate(long size) throws OutOfMemoryError {
    // 首先去寻找是否缓冲池有对应大小的内存块
    if (shouldPool(size)) {
      synchronized (this) {
        final LinkedList<WeakReference<MemoryBlock>> pool = bufferPoolsBySize.get(size);
        if (pool != null) {
          while (!pool.isEmpty()) {
            final WeakReference<MemoryBlock> blockReference = pool.pop();
            // 返回弱引用
            final MemoryBlock memory = blockReference.get();
            // 如果找到内存块，则直接返回
            if (memory != null) {
              assert (memory.size() == size);
              return memory;
            }
          }
          bufferPoolsBySize.remove(size);
        }
      }
    }
    // 新建数组，数组是属于堆内内存
    // 注意size是指定分配内存的字节数，一个long占有8个字节
    long[] array = new long[(int) ((size + 7) / 8)];
    // 注意这里array是数组的开始地址，但是数组会包含头部，长度为Platform.LONG_ARRAY_OFFSET
    // 真正的数据开始位置是在头部之后
    MemoryBlock memory = new MemoryBlock(array, Platform.LONG_ARRAY_OFFSET, size);
    if (MemoryAllocator.MEMORY_DEBUG_FILL_ENABLED) {
      memory.fill(MemoryAllocator.MEMORY_DEBUG_FILL_CLEAN_VALUE);
    }
    return memory;
  }
        
  private static final int POOLING_THRESHOLD_BYTES = 1024 * 1024;
  
  // 这里倾向于缓存大的内存块，因为大的内存块回收，会很影响jvm的性能
  private boolean shouldPool(long size) {
    return size >= POOLING_THRESHOLD_BYTES;
  }
}  
```

HeapMemoryAllocator的释放内存方法，并没有正在的释放，只是保存了它的弱引用。

```java
@Override
public void free(MemoryBlock memory) {
  final long size = memory.size();
  if (MemoryAllocator.MEMORY_DEBUG_FILL_ENABLED) {
    memory.fill(MemoryAllocator.MEMORY_DEBUG_FILL_FREED_VALUE);
  }
  if (shouldPool(size)) {
    synchronized (this) {
      LinkedList<WeakReference<MemoryBlock>> pool = bufferPoolsBySize.get(size);
      if (pool == null) {
        pool = new LinkedList<>();
        bufferPoolsBySize.put(size, pool);
      }
      pool.add(new WeakReference<>(memory));
    }
  } else {
    // Do nothing
  }
}
```



### 堆外分配

UnsafeMemoryAllocator调用Platform的allocateMemory方法，分配堆外内存。

调用Platform的freeMemory方法，释放堆外内存。

```java
public class UnsafeMemoryAllocator implements MemoryAllocator {

  @Override
  public MemoryBlock allocate(long size) throws OutOfMemoryError {
    // Platform 使用 unsafe分配堆外内存， 返回内存地址
    long address = Platform.allocateMemory(size);
    MemoryBlock memory = new MemoryBlock(null, address, size);
    if (MemoryAllocator.MEMORY_DEBUG_FILL_ENABLED) {
      memory.fill(MemoryAllocator.MEMORY_DEBUG_FILL_CLEAN_VALUE);
    }
    return memory;
  }
    
  @Override
  public void free(MemoryBlock memory) {
    assert (memory.obj == null) :
      "baseObject not null; are you trying to use the off-heap allocator to free on-heap memory?";
    if (MemoryAllocator.MEMORY_DEBUG_FILL_ENABLED) {
      memory.fill(MemoryAllocator.MEMORY_DEBUG_FILL_FREED_VALUE);
    }
    Platform.freeMemory(memory.offset);
  }
}
```

接下来继续研究Platform的源码，Platform其实是调用了Unsafe来分配和释放堆外内存的。

```java
public final class Platform {
  // 使用了Unsafe类来分配堆外内存
  private static final Unsafe _UNSAFE;
  // 通过反射获取Unsafe实例
  static {
    sun.misc.Unsafe unsafe;
    try {
      Field unsafeField = Unsafe.class.getDeclaredField("theUnsafe");
      unsafeField.setAccessible(true);
      unsafe = (sun.misc.Unsafe) unsafeField.get(null);
    } catch (Throwable cause) {
      unsafe = null;
    }
    _UNSAFE = unsafe;
  }
    
  // 调用Unsafe分配内存
  public static long allocateMemory(long size) {
    return _UNSAFE.allocateMemory(size);
  }

  // 调用Unsafe释放内存
  public static void freeMemory(long address) {
    _UNSAFE.freeMemory(address);
  }
}
```

 

## 内存管理

### 内存池

内存池负责管理内存，它只负责管理下列的数值，比如内存总大小，剩余大小，已使用的大小。但它不负责实际的内存分配。 

内存池的用处分为两种，一个是用作存储的，一部分用作执行任务的。

MemoryPool是内存池的基类，它只是简单的提供了，有锁访问内存信息的方法。

```scala
abstract class MemoryPool(lock: Object) {

  @GuardedBy("lock")
  private[this] var _poolSize: Long = 0
    
  // 返回内存的容量
  final def poolSize: Long = lock.synchronized {
    _poolSize
  }

  // 返回剩余的容量
  final def memoryFree: Long = lock.synchronized {
    _poolSize - memoryUsed
  }

  // 增大内存容量
  final def incrementPoolSize(delta: Long): Unit = lock.synchronized {
    require(delta >= 0)
    _poolSize += delta
  }

  // 减少内存容量
  final def decrementPoolSize(delta: Long): Unit = lock.synchronized {
    require(delta >= 0)
    require(delta <= _poolSize)
    require(_poolSize - delta >= memoryUsed)
    _poolSize -= delta
  }

  // 返回已经使用的内存大小
  def memoryUsed: Long
}
```



StorageMemoryPool 继承 MemoryPool，表示用来存储数据的内存池。 它支持堆外和堆内内存，由memoryMode参数指定。它提供了增加申请内存接口 acquireMemory。

```scala
private[memory] class StorageMemoryPool(
    lock: Object,
    memoryMode: MemoryMode       // 指定存储在堆内还是堆外
  ) extends MemoryPool(lock) with Logging {
    
    def acquireMemory(blockId: BlockId, numBytes: Long): Boolean = lock.synchronized {
        // 计算需要额外释放多少内存，才能满足需求
        val numBytesToFree = math.max(0, numBytes - memoryFree)
        acquireMemory(blockId, numBytes, numBytesToFree)
    }
     
    def acquireMemory(
      blockId: BlockId,
      numBytesToAcquire: Long,
      numBytesToFree: Long): Boolean = lock.synchronized {
        // 如果需要额外释放内存，则调用memoryStore将其他block的数据存储到磁盘
        if (numBytesToFree > 0) {
            memoryStore.evictBlocksToFreeSpace(Some(blockId), numBytesToFree, memoryMode)
        }
        // 检查是否已经有足够的内存
        val enoughMemory = numBytesToAcquire <= memoryFree
        // 如果有，则增加内存的使用量
        if (enoughMemory) {
            _memoryUsed += numBytesToAcquire
        }
        enoughMemory
    }
    
    // 返回已使用的内存容量
    override def memoryUsed: Long = lock.synchronized {
      _memoryUsed
    }
 
}
```



ExecutionMemoryPool继承 MemoryPool， 表示用来执行任务的内存池。它支持堆外和堆内内存，由memoryMode参数指定。它提供了增加申请内存接口 acquireMemory。

```scala
class ExecutionMemoryPool(
    lock: Object,
    memoryMode: MemoryMode
  ) extends MemoryPool(lock) with Logging {

  // 每个任务对应的已分配的内存大小
  private val memoryForTask = new mutable.HashMap[Long, Long]()

  // maybeGrowPool 函数， 在统一内存管理模式下，会从部分存储内存转到执行内存下
  // computeMaxPoolSize函数， 计算容量的最大值
  private[memory] def acquireMemory(
      numBytes: Long,
      taskAttemptId: Long,
      maybeGrowPool: Long => Unit = (additionalSpaceNeeded: Long) => Unit,
      computeMaxPoolSize: () => Long = () => poolSize): Long = lock.synchronized {
    
    while (true) {
      // 已经运行的task的数目
      val numActiveTasks = memoryForTask.keys.size
      // 当前任务已经分配的内存
      val curMem = memoryForTask(taskAttemptId)

      // 尝试增大内存容量
      maybeGrowPool(numBytes - memoryFree)

      // 实时计算容量的最大值
      val maxPoolSize = computeMaxPoolSize()
      // 计算每个任务的最大和最小的内存
      val maxMemoryPerTask = maxPoolSize / numActiveTasks
      val minMemoryPerTask = poolSize / (2 * numActiveTasks)

      // 计算此次任务最大的可以分配内存， 每个任务的内存不能超过maxMemoryPerTask
      val maxToGrant = math.min(numBytes, math.max(0, maxMemoryPerTask - curMem))
      // 计算可以分配的最大内存， 
      val toGrant = math.min(maxToGrant, memoryFree)

      // 如果当前内存不够，并且分配以后当前任务的内存仍然小于minMemoryPerTask
      // 那么等待资源释放
      if (toGrant < numBytes && curMem + toGrant < minMemoryPerTask) {
        logInfo(s"TID $taskAttemptId waiting for at least 1/2N of $poolName pool to be free")
        lock.wait()
      } else {
        // 如果内存足够，则返回已分配的内存数目
        memoryForTask(taskAttemptId) += toGrant
        return toGrant
      }
    }
    // 否则返回0
    0L  // Never reached
  }
  
  // 释放任务的内存
  def releaseMemory(numBytes: Long, taskAttemptId: Long): Unit = lock.synchronized {
    val curMem = memoryForTask.getOrElse(taskAttemptId, 0L)
    // 计算任务可以释放的内存
    var memoryToFree = if (curMem < numBytes) {
      logWarning(
        s"Internal error: release called on $numBytes bytes but task only has $curMem bytes " +
          s"of memory from the $poolName pool")
      curMem
    } else {
      numBytes
    }
    
    // 更新memoryForTask表
    if (memoryForTask.contains(taskAttemptId)) {
      memoryForTask(taskAttemptId) -= memoryToFree
      if (memoryForTask(taskAttemptId) <= 0) {
        memoryForTask.remove(taskAttemptId)
      }
    }
    // 通知等待内存释放的线程
    lock.notifyAll() // Notify waiters in acquireMemory() that memory has been freed
  }
}


  
```



### 内存池管理策略

MemoryManager 负责管理下列内存池

- onHeapStorageMemoryPool ： StorageMemoryPool，  堆内
- offHeapStorageMemoryPool   :   StorageMemoryPool， 堆外
- onHeapExecutionMemoryPool  :  ExecutionMemoryPool， 堆内
- offHeapExecutionMemoryPool  :  ExecutionMemoryPool， 堆外

对于内存的管理，spark支持两种策略，静态资源管理，动态资源管理。



#### 静态资源管理

静态资源管理会初始化各个内存池的大小，之后内存池的大小不能改变。内存分为两个用途，一个是缓存数据使用的，另一部分是任务执行使用的。静态资源管理不支持用于数据缓存的堆外内存。

StaticMemoryManager表示静态资源管理，它会根据spark的配置，来计算出各个内存池的容量。

getMaxStorageMemory方法返回缓存数据的内存容量

getMaxExecutionMemory方法返回执行任务的内存容量

```scala
private[spark] object StaticMemoryManager {

  private val MIN_MEMORY_BYTES = 32 * 1024 * 1024

  // 返回存储数据的内存容量
  private def getMaxStorageMemory(conf: SparkConf): Long = {
    // 获取
    val systemMaxMemory = conf.getLong("spark.testing.memory", Runtime.getRuntime.maxMemory)
    val memoryFraction = conf.getDouble("spark.storage.memoryFraction", 0.6)
    val safetyFraction = conf.getDouble("spark.storage.safetyFraction", 0.9)
    (systemMaxMemory * memoryFraction * safetyFraction).toLong
  }

  // 返回执行任务使用的内存容量
  private def getMaxExecutionMemory(conf: SparkConf): Long = {
    val systemMaxMemory = conf.getLong("spark.testing.memory", Runtime.getRuntime.maxMemory)
    val memoryFraction = conf.getDouble("spark.shuffle.memoryFraction", 0.2)
    val safetyFraction = conf.getDouble("spark.shuffle.safetyFraction", 0.8)
    (systemMaxMemory * memoryFraction * safetyFraction).toLong
  }
}
```



StaticMemoryManager对于内存的申请，分别对应 acquireStorageMemory提供申请缓存数据的内存，acquireExecutionMemory提供申请执行任务的内存。

```scala
class StaticMemoryManager(
    conf: SparkConf,
    maxOnHeapExecutionMemory: Long,
    override val maxOnHeapStorageMemory: Long,
    numCores: Int)
  extends MemoryManager(
    conf,
    numCores,
    maxOnHeapStorageMemory,
    maxOnHeapExecutionMemory) {

  def this(conf: SparkConf, numCores: Int) {
    this(
      conf,
      StaticMemoryManager.getMaxExecutionMemory(conf),  // 从配置文件读取和计算execution的内存容量
      StaticMemoryManager.getMaxStorageMemory(conf),    // 从配置文件读取和计算storage的内存容量
      numCores)
  }

  // 不支持堆外的存储内存，所以会将offHeapStorageMemoryPool的内存，
  // 转到offHeapExecutionMemoryPool里面
  offHeapExecutionMemoryPool.incrementPoolSize(offHeapStorageMemoryPool.poolSize)
  offHeapStorageMemoryPool.decrementPoolSize(offHeapStorageMemoryPool.poolSize)
  
  override def acquireStorageMemory(
      blockId: BlockId,
      numBytes: Long,
      memoryMode: MemoryMode): Boolean = synchronized {
    // 不支持堆外的存储内存
    require(memoryMode != MemoryMode.OFF_HEAP,
      "StaticMemoryManager does not support off-heap storage memory")
    // 如果申请内存大于堆内内存，则直接返回false，表示申请内存失败
    if (numBytes > maxOnHeapStorageMemory) {
      // Fail fast if the block simply won't fit
      logInfo(s"Will not store $blockId as the required space ($numBytes bytes) exceeds our " +
        s"memory limit ($maxOnHeapStorageMemory bytes)")
      false
    } else {
      // 向onHeapStorageMemoryPool申请内存
      onHeapStorageMemoryPool.acquireMemory(blockId, numBytes)
    }
  }
  
  override def acquireExecutionMemory(
      numBytes: Long,
      taskAttemptId: Long,
      memoryMode: MemoryMode): Long = synchronized {
    memoryMode match {
      case MemoryMode.ON_HEAP => onHeapExecutionMemoryPool.acquireMemory(numBytes, taskAttemptId)
      case MemoryMode.OFF_HEAP => offHeapExecutionMemoryPool.acquireMemory(numBytes, taskAttemptId)
    }
  }
  
}
```



#### 动态资源管理

动态资源管理的原理是，不严格划分缓存数据和任务执行的内存容量。当其中一个内存不足时，可以相互借用，共享内存。这样就可以提高内存的使用效率。

acquireStorageMemory方法提供了申请storage用途的内存。如果当前storage的内存不够，则试图向execution借用空闲的内存。

```scala
class UnifiedMemoryManager {
      
  override def acquireStorageMemory(
      blockId: BlockId,
      numBytes: Long,
      memoryMode: MemoryMode): Boolean = synchronized {
    // 根据内存在堆外还是堆内，返回对应的内存池
    val (executionPool, storagePool, maxMemory) = memoryMode match {
      case MemoryMode.ON_HEAP => (
        onHeapExecutionMemoryPool,
        onHeapStorageMemoryPool,
        maxOnHeapStorageMemory)
      case MemoryMode.OFF_HEAP => (
        offHeapExecutionMemoryPool,
        offHeapStorageMemoryPool,
        maxOffHeapStorageMemory)
    }
    // 申请内存超过了最大容量，返回false，表示没有申请成功
    if (numBytes > maxMemory) {
      logInfo(s"Will not store $blockId as the required space ($numBytes bytes) exceeds our " +
        s"memory limit ($maxMemory bytes)")
      return false
    }
    // 如果当前storage的内存池不够，则向execution的内存池借用
    if (numBytes > storagePool.memoryFree) {
      // 计算需要借用的内存大小，不能小于execution的空闲内存
      val memoryBorrowedFromExecution = Math.min(executionPool.memoryFree,
        numBytes - storagePool.memoryFree)
      // 减少execution内存池的容量
      executionPool.decrementPoolSize(memoryBorrowedFromExecution)
      // 增加storage内存池的容量
      storagePool.incrementPoolSize(memoryBorrowedFromExecution)
    }
    // 向storage内存池申请
    storagePool.acquireMemory(blockId, numBytes)
  }
}
```



acquireExecutionMemory方法提供了申请execution用途的内存。如果当execution的内存不够，它会借用storage的内存。

```scala
override private[memory] def acquireExecutionMemory(
    numBytes: Long,
    taskAttemptId: Long,
    memoryMode: MemoryMode): Long = synchronized {
  // 根据内存在堆外还是堆内，返回对应的内存池
  // storageRegionSize表示storage类型，当内存不足时，可使用的最高容量
  // maxMemory表示总的内存大小，包括storage和execution
  val (executionPool, storagePool, storageRegionSize, maxMemory) = memoryMode match {
    case MemoryMode.ON_HEAP => (
      onHeapExecutionMemoryPool,
      onHeapStorageMemoryPool,
      onHeapStorageRegionSize,
      maxHeapMemory)
    case MemoryMode.OFF_HEAP => (
      offHeapExecutionMemoryPool,
      offHeapStorageMemoryPool,
      offHeapStorageMemory,
      maxOffHeapMemory)
  }

  // 借用storage内存
  def maybeGrowExecutionPool(extraMemoryNeeded: Long): Unit = {
    if (extraMemoryNeeded > 0) {
      // 如果storage内存池有空闲内存，
      // 或者storage内存池的容量大于最低容量storageRegionSize
      val memoryReclaimableFromStorage = math.max(
        storagePool.memoryFree,
        storagePool.poolSize - storageRegionSize)
      if (memoryReclaimableFromStorage > 0) {
        // 尝试释放空间，有可能会将缓存的数据块，溢写到磁盘
        val spaceToReclaim = storagePool.freeSpaceToShrinkPool(
          math.min(extraMemoryNeeded, memoryReclaimableFromStorage))
        // 减少storage内存池的容量
        storagePool.decrementPoolSize(spaceToReclaim)
        // 增加execution内存池的容量
        executionPool.incrementPoolSize(spaceToReclaim)
      }
    }
  }

  // 计算最大的execution的内存容量
  def computeMaxExecutionPoolSize(): Long = {
    maxMemory - math.min(storagePool.memoryUsed, storageRegionSize)
  }

  // 向executionPool申请内存，maybeGrowExecutionPool和computeMaxExecutionPoolSize函数，
  // 会在每次尝试申请都会调用
  executionPool.acquireMemory(
    numBytes, taskAttemptId, maybeGrowExecutionPool, computeMaxExecutionPoolSize)
}
```



## 申请storage内存

上面介绍了内存的管理和分配，这里介绍了使用者的用法。我们以MemoryStore类为例，当spark缓存数据的时候，会申请storage类型的内存。

MemoryStore 的 putBytes 方法是将数据缓存到内存中。它申请内存的方法很简单，首先向memoryManager申请，如果memoryManager同意，直接调用_bytes函数，生成ChunkedByteBuffer。

```scala
class MemoryStore {
  
  def putBytes[T: ClassTag](
      blockId: BlockId,
      size: Long,
      memoryMode: MemoryMode,
      _bytes: () => ChunkedByteBuffer): Boolean = {
    // 申请storage内存
    if (memoryManager.acquireStorageMemory(blockId, size, memoryMode)) {
      // 生成ChunkedByteBuffer，里面存储着要缓存的数据
      val bytes = _bytes()
      assert(bytes.size == size)
      val entry = new SerializedMemoryEntry[T](bytes, memoryMode, implicitly[ClassTag[T]])
      entries.synchronized {
        entries.put(blockId, entry)
      }
      true
    } else {
      false
    }
  }
}
```



## 申请execution内存

Spark对于execution内存的申请，通过MemoryConsumer抽象类，统一了申请和释放内存的客户端接口，并且支持数据溢写到磁盘。接口如下

```java
public abstract class MemoryConsumer {
    
  // taskMemoryManager实例
  protected final TaskMemoryManager taskMemoryManager;
    
  // 释放内存，将数据溢写到磁盘
  public abstract long spill(long size, MemoryConsumer trigger) throws IOException;
    
  // 分配内存，返回形式为LongArray
  public LongArray allocateArray(long size) {}
    
  // 分配内存，返回形式为MemoryBlock
  protected MemoryBlock allocatePage(long required) {}
    
  // 释放内存
  public void freeMemory(long size) {}
```

MemoryConsumer的源码比较简单，它的申请内存接口，都是调用TaskMemoryManager的方法。



### TaskMemoryManager

TaskMemoryManager负责管理所有的MemoryConsumer。 当内存不够时，TaskMemoryManager会调用MemoryConsumer的spill方法，释放内存。

TaskMemoryManager同样也管理已分配的MemoryBlock，对于每个MemoryBlock都有一个唯一的pageNumber，表示它在TaskMemoryManager集合的位置。

TaskMemoryManager只负责任务执行的内存管理。它提供了allocatePage方法，分配内存 MemoryBlock

```java
public class TaskMemoryManager {
    
    // 表示内存是堆外还是堆内
    final MemoryMode tungstenMemoryMode;
    
    // MemoryConsumer集合
    private final HashSet<MemoryConsumer> consumers;
    
    // 保存分配的MemoryBlock
    private final MemoryBlock[] pageTable = new MemoryBlock[PAGE_TABLE_SIZE];

    // 索引为MemoryBlock的pageNumber，对应值为true，表示该MemoryBlock已经分配
    private final BitSet allocatedPages = new BitSet(PAGE_TABLE_SIZE);
    
    // 分配MemoryBlock
    public MemoryBlock allocatePage(long size, MemoryConsumer consumer) {
      assert(consumer != null);
      // 表示 consumer 的内存类型必须和TaskMemoryManager一致
      assert(consumer.getMode() == tungstenMemoryMode);
      // 申请内存，不能超过指定大小
      if (size > MAXIMUM_PAGE_SIZE_BYTES) {
        throw new IllegalArgumentException(
          "Cannot allocate a page with more than " + MAXIMUM_PAGE_SIZE_BYTES + " bytes");
      }

      // acquireExecutionMemory会去检测是否有足够的内存，
      // 还会尝试将MemoryConsumer溢写到磁盘来释放内存
      long acquired = acquireExecutionMemory(size, consumer);
      if (acquired <= 0) {
        return null;
      }
        
	 // 生成MemoryBlock的pageNumber
      final int pageNumber;
      synchronized (this) {
       // 寻找allocatedPages中值为0的索引，表示该pageNumber没有分配
        pageNumber = allocatedPages.nextClearBit(0);
        if (pageNumber >= PAGE_TABLE_SIZE) {
          releaseExecutionMemory(acquired, consumer);
          throw new IllegalStateException(
            "Have already allocated a maximum of " + PAGE_TABLE_SIZE + " pages");
        }
        allocatedPages.set(pageNumber);
      }
      MemoryBlock page = null;
      try {
        // 调用MemoryAllocator，来分配内存
        page = memoryManager.tungstenMemoryAllocator().allocate(acquired);
      } catch (OutOfMemoryError e) {
        logger.warn("Failed to allocate a page ({} bytes), try again.", acquired);
        synchronized (this) {
          // acquiredButNotUsed记录了申请失败的内存大小
          acquiredButNotUsed += acquired;
          // 因为申请失败了，所以pageNumber需要回收。
          allocatedPages.clear(pageNumber);
        }
        // 进行下一次尝试
        return allocatePage(size, consumer);
      }
      // 更新pageTable集合
      page.pageNumber = pageNumber;
      pageTable[pageNumber] = page;
      return page;
    }
}
```



注意到上面调用了acquireExecutionMemory方法。acquireExecutionMemory 方法会向 memoryManager 去申请内存，如果申请失败，会尝试释放内存。释放内存的原理是，从已申请的 MemoryConsumer中， 优先挑选出占用内存稍微大于申请内存的MemoryConsumer，如果没有 则按照占用内存从大到小的顺序释放。

```java
public long acquireExecutionMemory(long required, MemoryConsumer consumer) {
  assert(required >= 0);
  assert(consumer != null);
  MemoryMode mode = consumer.getMode();
  synchronized (this) {
    // 向memoryManager申请execution内存， 返回申请的内存容量
    long got = memoryManager.acquireExecutionMemory(required, taskAttemptId, mode);

    // 如果获得内存小于申请大小，则需要MemoryConsumer溢写，释放内存
    if (got < required) {
      // 记录MemoryConsumer的内存量
      // Key为内存使用量， Value为对应的MemoryConsumer的列表
      TreeMap<Long, List<MemoryConsumer>> sortedConsumers = new TreeMap<>();
      for (MemoryConsumer c: consumers) {
        if (c != consumer && c.getUsed() > 0 && c.getMode() == mode) {
          long key = c.getUsed();
          List<MemoryConsumer> list =
              sortedConsumers.computeIfAbsent(key, k -> new ArrayList<>(1));
          list.add(c);
        }
      }
        
      // 尝试挑选MemoryConsumer溢写， 直到申请内存满足或者所有的MemoryConsumer已经溢写
      while (!sortedConsumers.isEmpty()) {
        // required - got 表示还差多少内存
        // 寻找刚好内存大于或等于需求值的MemoryConsumer
        Map.Entry<Long, List<MemoryConsumer>> currentEntry =
          sortedConsumers.ceilingEntry(required - got);
        // 如果没有找到，则返回最后的一项，也就是最大的一项
        if (currentEntry == null) {
          currentEntry = sortedConsumers.lastEntry();
        }
        // 获取对应的MemoryConsumer列表
        List<MemoryConsumer> cList = currentEntry.getValue();
        // 从MemoryConsumer列表取出一个元素
        MemoryConsumer c = cList.remove(cList.size() - 1);
        if (cList.isEmpty()) {
          sortedConsumers.remove(currentEntry.getKey());
        }
          
        try {
          // 调用该MemoryConsumer的spill溢写，释放内存
          long released = c.spill(required - got, consumer);
          if (released > 0) {
            // 继续向memoryManager申请execution内存， 并且更新已获取的内存容量got
            got += memoryManager.acquireExecutionMemory(required - got, taskAttemptId, mode);
            // 如果已经申请到了足够的内存，则跳出循环
            if (got >= required) {
              break;
            }
          }
        } catch (ClosedByInterruptException e) {
          // This called by user to kill a task (e.g: speculative task).
          logger.error("error while calling spill() on " + c, e);
          throw new RuntimeException(e.getMessage());
        } catch (IOException e) {
          logger.error("error while calling spill() on " + c, e);
          throw new OutOfMemoryError("error while calling spill() on " + c + " : "
            + e.getMessage());
        }
      }
    }

    // 如果所有的MemoryConsumer都已经溢写，仍旧没有满足
    // 那么只能将申请者MemoryConsumer溢写
    if (got < required) {
      try {
        // 当前MemoryConsumer溢写
        long released = consumer.spill(required - got, consumer);
        if (released > 0) {
          // 继续向memoryManager申请execution内存， 并且更新已获取的内存容量got
          got += memoryManager.acquireExecutionMemory(required - got, taskAttemptId, mode);
        }
      } catch (ClosedByInterruptException e) {
        logger.error("error while calling spill() on " + consumer, e);
        throw new RuntimeException(e.getMessage());
      } catch (IOException e) {
        logger.error("error while calling spill() on " + consumer, e);
        throw new OutOfMemoryError("error while calling spill() on " + consumer + " : "
          + e.getMessage());
      }
    }
    // 添加到consumers列表
    consumers.add(consumer);
    logger.debug("Task {} acquired {} for {}", taskAttemptId, Utils.bytesToString(got), consumer);
    return got;
  }
}
```



### ShuffleExternalSorter例子

这里以ShuffleExternalSorter为例，它继承 MemoryConsumer， 负责shuffle排序用的。当它排序的时候，会使用到execution内存。

acquireNewPageIfNecessary方法会申请内存，这里面就是调用了MemoryConsumer的allocatePage方法。

```scala
class ShuffleExternalSorter extends MemoryConsumer {

  private void acquireNewPageIfNecessary(int required) {
    if (currentPage == null ||
      pageCursor + required > currentPage.getBaseOffset() + currentPage.size() ) {
      调用
      currentPage = allocatePage(required);
      pageCursor = currentPage.getBaseOffset();
      allocatedPages.add(currentPage);
    }
  }
}


public abstract class MemoryConsumer {

  protected final TaskMemoryManager taskMemoryManager;
    
  protected MemoryBlock allocatePage(long required) {
    // 向taskMemoryManager申请分配内存
    MemoryBlock page = taskMemoryManager.allocatePage(Math.max(pageSize, required), this);
    if (page == null || page.size() < required) {
      long got = 0;
      if (page != null) {
        got = page.size();
        taskMemoryManager.freePage(page, this);
      }
      taskMemoryManager.showMemoryUsage();
      throw new OutOfMemoryError("Unable to acquire " + required + " bytes of memory, got " + got);
    }
    used += page.size();
    return page;
  }
}
```

