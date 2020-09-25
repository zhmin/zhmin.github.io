---
title: Spark 内存管理
date: 2019-01-15 22:50:44
tags: spark, memory
categories: spark
---

## 前言

spark自己实现了一套内存的管理，为了精准的控制内存。因为JVM的垃圾回收不可控制，尤其是大内存的回收，会造成程序暂停长时间。spark为了避免这种情况，实现了内存的管理，尤其是针对大内存。对于大内存，spark会使用内存池的方法，避免了垃圾回收的次数，提高了程序的稳定性。目前spark支持堆内内存分配和堆外内存分配。

## 内存分配

### 内存块

spark分配内存是以内存块为单位的，内存块由`MemoryBlock`表示。

```java
public class MemoryBlock extends MemoryLocation {
  private final long length;     //  内存中的数据大小
}
```

`MemoryLocation`表示内存地址，当需要遍历存储的字节数据时，是从`obj`的位置加上 `offset`偏移量的位置

```java
public class MemoryLocation {

  @Nullable
  Object obj;       // 内存的起始地址

  long offset;     // 数据的起始偏移位置
}
```



### 内存分配方式

Spark 根据分配的内存位置（堆内，堆外），分为两种方式。

1. `HeapMemoryAllocator`类实现了堆内分配内存。
2. `UnsafeMemoryAllocator`则支持在堆外分配内存。

### 堆内分配

`HeapMemoryAllocator`自己实现了一个缓存池，不过它只复用大尺寸的内存。可以看到当内存块大小大于1MB的时候，才会进入缓存池。那为什么仅仅对大内存才复用，因为大内存的分配和回收都更加容易会造成 jvm 的不稳定。

```java
public class HeapMemoryAllocator implements MemoryAllocator {
  
  // 缓存池，Key的内存块的大小，Value为内存块的弱引用
  @GuardedBy("this")
  private final Map<Long, LinkedList<WeakReference<MemoryBlock>>> bufferPoolsBySize =
    new HashMap<>();
    
  private static final int POOLING_THRESHOLD_BYTES = 1024 * 1024;
    
  // 这里倾向于缓存大的内存块，因为大的内存块回收，会很影响jvm的性能
  private boolean shouldPool(long size) {
    return size >= POOLING_THRESHOLD_BYTES;
  }
}
```

这里还需要注意到缓存池，它使用弱引用来保存`MemoryBlock`。这样的好处时，当内存不足时，jvm 会自动清除那些没有用的`MemoryBlock`，而不需要我们额外的处理。

`HeapMemoryAllocator`使用`long`数组来表示`MemoryBlock`的，所以分配内存时，会计算long的数组大小。

### 堆外分配

`UnsafeMemoryAllocator`负责分配堆外内存，它的原理非常简单。仅仅是调用`Unsafe.allocateMemory`方法进行分配，调用`Unsafe.freeMemory`方法进行释放。这里需要注意下它返回的`MemoryBlock`的`object`为 null，表示堆外内存。

```java
public class UnsafeMemoryAllocator implements MemoryAllocator {

  @Override
  public MemoryBlock allocate(long size) throws OutOfMemoryError {
    // Platform 使用 unsafe分配堆外内存， 返回内存地址
    long address = Platform.allocateMemory(size);
    MemoryBlock memory = new MemoryBlock(null, address, size);
    return memory;
  }
}
```



## 内存池

内存池负责管理内存的使用量，它只负责管理内存总大小，已使用内存的大小这些数值，由抽象类`MemoryPool`表示。

内存池根据用途分为两种，

* 一个是用作存储的，由`StorageMemoryPool `表示
* 一部分用作执行任务的，由`ExecutionMemoryPool`表示

这里需要提下内存池只是负责管理内存容量，并不会实际的分配内存。至于为什么要这样设计，是因为并发的原因。内存池的操作都使用了锁，支持多线程运行，并且内存的分配是相对耗时的，所以为了减少锁的占用时间，就将耗时的内存操作移到外面了。



### 数据存储内存池

`StorageMemoryPool` 继承 `MemoryPool`，用来存储数据的内存池。 它支持堆外和堆内内存，由`memoryMode`参数指定。当用户需要申请内存时，需要提供 blockId，用于标识数据。

它的原理也很简单，如果该`MemoryPool`的内存足够，那么会更新`MemoryPool`的已使用内存的数值。真正的分配是由客户端自己提供。如果内存不足，它会试着将之前的 block 放入磁盘存储。

### 任务执行内存池

`ExecutionMemoryPool`继承 `MemoryPool`， 表示用来执行任务的内存池。它支持堆外和堆内内存，由`memoryMode`参数指定。当用户需要申请内存时，需要提供 taskId，用于表示此次任务。

`ExecutionMemoryPool`的原理相对复杂一些，它涉及到了借的概念。我们知道程序运行是需要占用内存的，它的优先级是比数据存储还要高。所以当执行内存不足时，它会尝试从数据存储内存池里面借一些使用。

`ExecutionMemoryPool`在申请内存时，需要提供两个函数。

1. `maybeGrowPool`函数，负责动态提升内存的总大小。
2. `computeMaxPoolSize`函数，负责内存池的大小，通过它可以实现内存的借和还。



## 内存池管理策略

`MemoryManager` 负责管理下面四个内存池

```scala
private[spark] abstract class MemoryManager(
    conf: SparkConf,
    numCores: Int,
    onHeapStorageMemory: Long,
    onHeapExecutionMemory: Long) extends Logging {

  @GuardedBy("this")
  protected val onHeapStorageMemoryPool = new StorageMemoryPool(this, MemoryMode.ON_HEAP)  // 堆内数据存储
  @GuardedBy("this")
  protected val offHeapStorageMemoryPool = new StorageMemoryPool(this, MemoryMode.OFF_HEAP)  // 堆外数据存储
  @GuardedBy("this")
  protected val onHeapExecutionMemoryPool = new ExecutionMemoryPool(this, MemoryMode.ON_HEAP)  // 堆内任务执行
  @GuardedBy("this")
  protected val offHeapExecutionMemoryPool = new ExecutionMemoryPool(this, MemoryMode.OFF_HEAP)  // 堆外任务执行
    
}
```



`MemoryManager`的构造函数，指定了堆内数据存储内存池的大小，和堆内任务执行内存池的大小。至于堆外的内存池大小如下所示：

```scala
// MEMORY_OFFHEAP_SIZE 表示 spark.memory.offHeap.size 配置，表示堆外的总大小
protected[this] val maxOffHeapMemory = conf.get(MEMORY_OFFHEAP_SIZE)
// spark.memory.storageFraction 指定存储占用的比例
protected[this] val offHeapStorageMemory = (maxOffHeapMemory * conf.getDouble("spark.memory.storageFraction", 0.5)).toLong
// 设置堆外内存池的大小
offHeapExecutionMemoryPool.incrementPoolSize(maxOffHeapMemory - offHeapStorageMemory)
offHeapStorageMemoryPool.incrementPoolSize(offHeapStorageMemory)
```



Spark 支持两种管理策略来平衡各个内存池的大小，静态资源管理和动态资源管理。



### 静态资源管理

静态资源管理不支持堆外数据存储，它只管理三个内存池。在开始的时候，它会初始化各个内存池的大小，之后内存池的大小不能改变，也就是说内存池之间是不能相互借用的。

```scala
private[spark] object StaticMemoryManager {

  private val MIN_MEMORY_BYTES = 32 * 1024 * 1024  // 32MB

  // 返回堆内存储数据的内存池容量
  private def getMaxStorageMemory(conf: SparkConf): Long = {
    val systemMaxMemory = conf.getLong("spark.testing.memory", Runtime.getRuntime.maxMemory)
    val memoryFraction = conf.getDouble("spark.storage.memoryFraction", 0.6)
    val safetyFraction = conf.getDouble("spark.storage.safetyFraction", 0.9)
    (systemMaxMemory * memoryFraction * safetyFraction).toLong
  }

  // 返回堆内执行任务使用的内存池容量
  private def getMaxExecutionMemory(conf: SparkConf): Long = {
    val systemMaxMemory = conf.getLong("spark.testing.memory", Runtime.getRuntime.maxMemory)
    val memoryFraction = conf.getDouble("spark.shuffle.memoryFraction", 0.2)
    val safetyFraction = conf.getDouble("spark.shuffle.safetyFraction", 0.8)
    (systemMaxMemory * memoryFraction * safetyFraction).toLong
  }
}
```

因为静态资源管理不支持堆外数据存储，所以它会将堆外的内存都放入到堆外的执行内存池里。

### 动态资源管理

动态资源管理的原理是，不严格划分缓存数据和任务执行的内存容量。存储内存池和执行内存池相互可以借用，这样就可以提高内存的使用效率。它的内存池初始化如下所示：

```scala
private val RESERVED_SYSTEM_MEMORY_BYTES = 300 * 1024 * 1024  // 300MB 的保留内存
val systemMemory = Runtime.getRuntime.maxMemory // 计算Jvm的内存总量
val usableMemory = systemMemory - RESERVED_SYSTEM_MEMORY_BYTES  // 出去保留内存，可以使用的内存大小
val memoryFraction = conf.getDouble("spark.memory.fraction", 0.6)
val maxMemory = (usableMemory * memoryFraction).toLong // 用于堆内内存池的大小，为60%
val onHeapStorageRegionSize = (maxMemory * conf.getDouble("spark.memory.storageFraction", 0.5)).toLong // 堆内存储内存为50%
val onHeapExecutionMemory = maxMemory - onHeapStorageRegionSize
```



用户申请存储内存时，如果当前内存池不够，则会向运行内存池借用。

用户申请运行内存时，如果当前内存池不够，则会向存储内存池借用。因为运行内存的优先级高，还会进一步会要求存储内存池持久化它的block，来获得空闲空间。

```scala
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
```



## 申请内存示例

上面介绍了内存的管理和分配，这里介绍了使用者的用法。我们以MemoryStore类为例，当spark缓存数据的时候，会申请storage类型的内存。

```scala
memoryStore.putBytes(blockId, size, memoryMode, () => {
    // 如果申请的是堆外内存，并且原始的数据存放在堆内，那么就需要申请堆外内存，并且执行拷贝
    if (memoryMode == MemoryMode.OFF_HEAP &&
        bytes.chunks.exists(buffer => !buffer.isDirect)) {
        bytes.copy(Platform.allocateDirectBuffer)
    } else {
        bytes
    }
})
```

这里需要注意下有个回调函数，用来分配内存的。再接着看`MemoryStore` 的 `putBytes` 方法。它申请内存的方法很简单，首先向memoryManager申请，如果memoryManager同意，直接调用_bytes函数，生成ChunkedByteBuffer。

```scala
class MemoryStore {
  
  def putBytes[T: ClassTag]( blockId: BlockId, size: Long, memoryMode: MemoryMode, _bytes: () => ChunkedByteBuffer): Boolean = {
    // 申请storage内存
    if (memoryManager.acquireStorageMemory(blockId, size, memoryMode)) {
      // 生成ChunkedByteBuffer，里面存储着要缓存的数据
      val bytes = _bytes()
      assert(bytes.size == size)
      ......
      true
    } else {
      false
    }
  }
}
```

可以看到用户向内存池申请时，是需要自己负责内存分配的。

