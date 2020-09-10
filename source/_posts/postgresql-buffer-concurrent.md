---
title: postgresql 缓存池并发设计
date: 2020-09-08 21:08:52
tags: postgresql, buffer
categories: postgresql
---

## 前言

postgresql 对于缓存池的并发控制，比较复杂。下面通过一步步的优化，来讲述 postgresql 的设计思路。它为了提升锁的性能，大量使用了 CAS 操作，只有在操作比较慢的时候，才会加入读写锁。在继续阅读之前，需要熟悉它的存储设计，可以参考这篇文章 {% post_link  postgresql-storage-architecture postgresql 存储设计 %} 。

## 设计思路推演

首先考虑下单线程的情况，我们先要查找缓存池是否已经包含了要读取的page，如果包含了则直接返回该 buffer。如果没有，需要从磁盘加载到缓存池中，然后返回。下面是通过一步步的加锁，来实现并发的控制。



### Hash 表锁

hash 表保存了 page 到 buffer 的对应关系，很明显所有的读取都需要从 hash 表查找开始，我们可以从 控制 hash 表的锁来开始。假设现在只有 hash 分区锁，我们写的程序应该是这样的

```c
// 获取分区 id，page_number 表示要获取的 page 标识
partition_id = get_hash_parition(page_number);
// 对此分区加读锁
lock_partition_read(hash_table, partition_id);
// 查找该 page 是否存在缓存
buffer_id, found = lookup(hash_table, page_number);

if (found) {
    // 如果找到该缓存，则返回
    ublock(hash_table, partition);
    return buffer_id;
}

// 如果没有对应的缓存，则需要从缓存池中，找到替换的位置
free_buffer_id = find_victim_buffer();
// 获取旧有位置的缓存对应哪个 page number
free_page_number = get_page_number(free_buffer_id);
// 对其所在的分区加写锁
free_partition_id = get_hash_parition(free_page_number);
lock_partition_write(hash_table, free_partition_id);
lock_partition_write(hash_table, partition_id);
// 删除旧有的缓存
delete(hash_table, free_page_number);
// 从磁盘读取数据到缓存，并且添加到 hash 表
read_from_disk(page_number, free_buffer_id);
insert(hash_table, pageNumber, free_buffer_id);

// 释放分区锁
unlock_partition(hash_table, free_buffer_id);
unlock_partition(hash_table, partition_id);
return free_buffer_id;
```



### Pin Buffer

其中 find_victim_buffer 函数负责挑选出可被替换的 buffer，它的原理是找到 pin count 等于 0 的 buffer。所以这里面会有个问题，当我们查找到数据后，需要及时的 pin 操作，不然有可能会被替换掉，造成读取的数据不是想要的 page number。

```c
// 获取分区 id，page_number 表示要获取的 page 标识
partition_id = get_hash_parition(page_number);
// 对此分区加读锁
lock_partition_read(hash_table, partition_id);
// 查找该 page 是否存在缓存
buffer_id, found = lookup(hash_table, page_number);

if (found) {
    // 需要及时 pin 操作
    pin_buffer(buffer_id)
    ublock(hash_table, partition);
    return buffer_id;
}

// 如果没有对应的缓存，则需要从缓存池中，找到替换的位置
free_buffer_id = find_victim_buffer();
// 获取旧有位置的缓存对应哪个 page number
free_page_number = get_page_number(free_buffer_id);
// 及时 pin 操作
pin_buffer(free_buffer_id);

// 对其所在的分区加写锁
free_partition_id = get_hash_parition(free_page_number);
lock_partition_write(hash_table, free_partition_id);
lock_partition_write(hash_table, partition_id);
// 删除旧有的缓存
delete(hash_table, free_page_number);
// 从磁盘读取数据到缓存，并且添加到 hash 表
read_from_disk(page_number, free_buffer_id);
insert(hash_table, pageNumber, free_buffer_id);

// 释放分区锁
unlock_partition(hash_table, free_buffer_id);
unlock_partition(hash_table, partition_id);
return free_buffer_id;
```

### 查找空闲Buffer优化

我们观察上述代码，如果我们没有找到对应的 buffer，那么需要从buffer数组中挑选出一个合适的，这个步骤是很花费时间的。所以会造成长时间的加锁，并且hash表的一个分区会包含了多个 buffer，这样会造成性能影响。所以 在执行这一步之前，我们可以将分区锁释放。不过这样会遇到一个问题，如下所示

```c
// 获取分区 id，page_number 表示要获取的 page 标识
partition_id = get_hash_parition(page_number);
// 对此分区加读锁
lock_partition_read(hash_table, partition_id);
// 查找该 page 是否存在缓存
buffer_id, found = lookup(hash_table, page_number);

if (found) {
    // 需要及时 pin 操作
    pin_buffer(buffer_id)
    ublock(hash_table, partition);
    return buffer_id;
}

// 释放锁，防止长时间占住分区锁
unlock_partition(hash_table, partition_id);

// 如果没有对应的缓存，则需要从缓存池中，找到替换的位置
free_buffer_id = find_victim_buffer();
// 获取旧有位置的缓存对应哪个 page number
free_page_number = get_page_number(free_buffer_id);
// 及时 pin 操作
pin_buffer(free_buffer_id);

// 对其所在的分区加写锁
free_partition_id = get_hash_parition(free_page_number);

// 步骤1
lock_partition_write(hash_table, free_partition_id);
lock_partition_write(hash_table, partition_id);


// 删除旧有的缓存
delete(hash_table, free_page_number);
// 从磁盘读取数据到缓存，并且添加到 hash 表
read_from_disk(page_number, free_buffer_id);
insert(hash_table, pageNumber, free_buffer_id);

// 释放分区锁
unlock_partition(hash_table, free_buffer_id);
unlock_partition(hash_table, partition_id);
return free_buffer_id;
```

### 避免重复读取

我们假设有两个线程都跑到了 步骤1，那么就会产生两个 buffer 都是保存了同一份 Page 的数据。为了避免这一情况，我们在执行步骤1之后，需要检查一下。

```c
// 获取分区 id，page_number 表示要获取的 page 标识
partition_id = get_hash_parition(page_number);
// 对此分区加读锁
lock_partition_read(hash_table, partition_id);
// 查找该 page 是否存在缓存
buffer_id, found = lookup(hash_table, page_number);

if (found) {
    // 需要及时 pin 操作
    pin_buffer(buffer_id)
    ublock(hash_table, partition);
    return buffer_id;
}

// 释放锁，防止长时间占住分区锁
unlock_partition(hash_table, partition_id);

// 如果没有对应的缓存，则需要从缓存池中，找到替换的位置
free_buffer_id = find_victim_buffer();
// 获取旧有位置的缓存对应哪个 page number
free_page_number = get_page_number(free_buffer_id);
// 及时 pin 操作
pin_buffer(free_buffer_id);

// 对其所在的分区加写锁
free_partition_id = get_hash_parition(free_page_number);

// 步骤1
lock_partition_write(hash_table, free_partition_id);
lock_partition_write(hash_table, partition_id);


// 增加查看是否有别的进程已经在读取磁盘数据了
buffer_id, found = lookup(hash_table, page_number);
if (found) {
    return buffer_id;
}

// 删除旧有的缓存
delete(hash_table, free_page_number);
// 从磁盘读取数据到缓存，并且添加到 hash 表
read_from_disk(page_number, free_buffer_id);
insert(hash_table, pageNumber, free_buffer_id);

// 释放分区锁
unlock_partition(hash_table, free_buffer_id);
unlock_partition(hash_table, partition_id);
return free_buffer_id;
```



### 磁盘读取优化

同样从磁盘读取数据的操作也会很花费时间，为了避免长时间占用锁，我们需要在执行之前释放相关的锁。如果要实现这一步，需要设置一个 valid 标记位，表示数据是否成功的被磁盘读取。所以我们在哈希表中找到缓存后，还需要分辨数据是否完成磁盘读取，并且我们还需要额外的锁（称为 io_lock），来保证同步等待。

```c
// 获取分区 id，page_number 表示要获取的 page 标识
partition_id = get_hash_parition(page_number);
// 对此分区加读锁
lock_partition_read(hash_table, partition_id);
// 查找该 page 是否存在缓存
buffer_id, found = lookup(hash_table, page_number);

if (found) {
    pin_buffer(buffer_id);
    unlock_partition(hash_table, partition_id);
    while(! is_valid(buffer_id)) {
        wait_io_lock(buffer_id)
    }
    return buffer_id;
}

// 释放锁，防止长时间占住分区锁
unlock_partition(hash_table, partition_id);

// 如果没有对应的缓存，则需要从缓存池中，找到替换的位置
free_buffer_id = find_victim_buffer();
// 获取旧有位置的缓存对应哪个 page number
free_page_number = get_page_number(free_buffer_id);
// 及时 pin 操作
pin_buffer(free_buffer_id);

// 对其所在的分区加写锁
free_partition_id = get_hash_parition(free_page_number);

// 步骤1
lock_partition_write(hash_table, free_partition_id);
lock_partition_write(hash_table, partition_id);


// 增加查看是否有别的进程已经在读取磁盘数据了
buffer_id, found = lookup(hash_table, page_number);
if (found) {
    return buffer_id;
}

// 删除旧有的缓存
delete(hash_table, free_page_number);

// 设置该缓存无效
set_buffer_invalid(free_buffer_id);

// 并且添加到 hash 表
insert(hash_table, pageNumber, free_buffer_id);

// 释放分区锁
unlock_partition(hash_table, free_buffer_id);
unlock_partition(hash_table, partition_id);

// 加锁
lock_io(free_buffer_id);
// 如果该缓存已经成功读取完了，那么直接返回。
while (! is_valid(free_buffer_id)) {
    // 否则从磁盘读取
    read_from_disk(page_number, free_buffer_id);
    set_buffer_valid(free_buffer_id);
}
unlock_io(free_buffer_id)
return free_buffer_id;
```



### Valid标记位

现在基本的流程确定好了，不过 postgresql 认为加载磁盘是很慢的操作，是可以被终止的。所以当检测到一个 buffer 为 invalid 时，可能对应着两种情况，一种是其它进程正在进行读取，一种是之前的读取被终止了。所以当发现 buffer invalid 的时候，都需要主动去磁盘读取。但是这样会造成并发重复读取，所以还需要提供一个锁，专门负责这一块的同步，称为 io_lock。

```c
// 获取分区 id，page_number 表示要获取的 page 标识
partition_id = get_hash_parition(page_number);
// 对此分区加读锁
lock_partition_read(hash_table, partition_id);
// 查找该 page 是否存在缓存
buffer_id, found = lookup(hash_table, page_number);

if (found) {
    pin_buffer(buffer_id);
    unlock_partition(hash_table, partition_id);
    // 加锁
    lock_io(free_buffer_id);
    // 如果该缓存已经成功读取完了，那么直接返回。
    while (! is_valid(free_buffer_id)) {
        // 否则从磁盘读取
        read_from_disk(page_number, free_buffer_id);
        set_buffer_valid(free_buffer_id);
    }
    unlock_io(free_buffer_id)
    return buffer_id;
}

// 释放锁，防止长时间占住分区锁
unlock_partition(hash_table, partition_id);

// 如果没有对应的缓存，则需要从缓存池中，找到替换的位置
free_buffer_id = find_victim_buffer();
// 获取旧有位置的缓存对应哪个 page number
free_page_number = get_page_number(free_buffer_id);
// 及时 pin 操作
pin_buffer(free_buffer_id);

// 对其所在的分区加写锁
free_partition_id = get_hash_parition(free_page_number);

// 步骤1
lock_partition_write(hash_table, free_partition_id);
lock_partition_write(hash_table, partition_id);


// 增加查看是否有别的进程已经在读取磁盘数据了
buffer_id, found = lookup(hash_table, page_number);
if (found) {
    return buffer_id;
}

// 删除旧有的缓存
delete(hash_table, free_page_number);

// 设置该缓存无效
set_buffer_invalid(free_buffer_id);

// 并且添加到 hash 表
insert(hash_table, pageNumber, free_buffer_id);

// 释放分区锁
unlock_partition(hash_table, free_buffer_id);
unlock_partition(hash_table, partition_id);

// 加锁
lock_io(free_buffer_id);
// 如果该缓存已经成功读取完了，那么直接返回。
while (! is_valid(free_buffer_id)) {
    // 否则从磁盘读取
    read_from_disk(page_number, free_buffer_id);
    set_buffer_valid(free_buffer_id);
}
unlock_io(free_buffer_id)
return free_buffer_id;
```



## 锁实现

上面一步步推导出了最后的并发流程，接下来依次介绍这些锁的实现。



### hash 表分区锁

postgresql 将 hash 表分成多个区，每个区都对应了一个读写锁。

```c
BufferTag	newTag;  // 表示要查找的 page
newHash = BufTableHashCode(&newTag);  // 计算 hash 值
newPartitionLock = BufMappingPartitionLock(newHash);   // 根据hash值找到对应的分区锁
LWLockAcquire(newPartitionLock, LW_SHARED); // 加读锁
```

`BufMappingPartitionLock`函数分为两步，首先是找到对应的分区。然后是找到该分区对应的锁。

```c
// 这里只是简单的取余操作，找到对应的分区。NUM_BUFFER_PARTITIONS 表示分区数目
#define BufTableHashPartition(hashcode)  ((hashcode) % NUM_BUFFER_PARTITIONS) 

// postgresql 分配了一个大的lwlock 数组，分区锁占用了其中一部分，从 BUFFER_MAPPING_LWLOCK_OFFSET 位置开始
#define BufMappingPartitionLock(hashcode) 	(&MainLWLockArray[BUFFER_MAPPING_LWLOCK_OFFSET + BufTableHashPartition(hashcode)].lock)
```



### buffer header 锁

`BufferDesc` 包含了`buffer`的头部信息，也包含了相关的锁

```c
typedef struct BufferDesc
{
	BufferTag	tag;			// 缓存哪个文件的block
	int			buf_id;			// buffer id
	pg_atomic_uint32 state;		// 状态值

	int			wait_backend_pid;	/* backend PID of pin-count waiter */
	int			freeNext;		// 指向下个空闲位置

	LWLock		content_lock;	// 读写锁，用来控制内容的访问
} BufferDesc;
```

`state`字段是一个 atomic 类型的值，它的更新非常频繁。它有一个标记位`BM_LOCKED`，作为锁的实现。postgresql 使用了自旋 + CAS 操作来实现乐观锁。

```c
uint32 LockBufHdr(BufferDesc *desc)
{
    // 用于自适应控制自旋间隙
	SpinDelayStatus delayStatus;
	uint32		old_buf_state;
    // 初始化
	init_local_spin_delay(&delayStatus);

	while (true)
	{
		// CAS 操作，来设置BM_LOCKED标记位
		old_buf_state = pg_atomic_fetch_or_u32(&desc->state, BM_LOCKED);
		// 如果之前BM_LOCKED标记位没有设置，说明之前没有人加锁，现在我们已经成功加锁了。
		if (!(old_buf_state & BM_LOCKED))
			break;
        // 如果没有加锁成功，则判断是否需要延迟
		perform_spin_delay(&delayStatus);
	}
	finish_spin_delay(&delayStatus);
	return old_buf_state | BM_LOCKED;
}
```

释放锁就很简单，只是简单的取消标记位`BM_LOCKED`

```c
#define UnlockBufHdr(desc, s)	\
	do {	\
		pg_write_barrier(); \
		pg_atomic_write_u32(&(desc)->state, (s) & (~BM_LOCKED)); \
	} while (0)
```



### Pin Buffer

pin 操作也是基于 CAS 操作，`state`字段中保存了 pin count。不过它并没有使用`BM_LOCKED`标记位，因为 pin 操作非常频繁，postgresql 做了进一步的优化。它分为两种场景，一种是pin操作之前没有获取到锁，一种是已经获取到了锁。

```c
// 调用之前没有获取到锁
static bool PinBuffer(BufferDesc *buf, BufferAccessStrategy strategy)
{
	Buffer		b = BufferDescriptorGetBuffer(buf);
    old_buf_state = pg_atomic_read_u32(&buf->state);
    for (;;)
	{
        // 如果之前有锁，那么需要等待锁释放BM_LOCKED标记位
        if (old_buf_state & BM_LOCKED)
            // 注意到这里返回的值，肯定不会包含
            old_buf_state = WaitBufHdrUnlocked(buf);
        // 计算出新值
        buf_state = old_buf_state;
        buf_state += BUF_REFCOUNT_ONE;
        // 调用CAS操作，需要注意到 old_buf_state 不会包含BM_LOCKED标记位，所以永远不会更新带有BM_LOCKED标记位的值
        if (pg_atomic_compare_exchange_u32(&buf->state, &old_buf_state, buf_state)) {
            break;
        }
    }
}
```

第二种情况适用于获取到了锁，这种情况只是简单的修改state值就行。

```c
// 调用之前已经获取到了锁
static void PinBuffer_Locked(BufferDesc *buf)
{
	uint32		buf_state;
    // 获取state值
	buf_state = pg_atomic_read_u32(&buf->state);
	Assert(buf_state & BM_LOCKED);
    // 修改state值
	buf_state += BUF_REFCOUNT_ONE;
	UnlockBufHdr(buf, buf_state);
}
```

pin 操作只是保证了对应的 buffer 不会被替换掉。如果需要保证 buffer 里面的数据并发修改，则需要加入 buffer 的读写锁。buffer 读写锁是`BufferDesc`的`content_lock`字段，操作比较简单，这里不再详述。

### Buffer IO 锁

为了控制 IO 的同步，postgresql 使用了 buffer_io_lock 锁来控制。因为 IO 操作非常耗时，所以这里就不在使用了乐观锁。

```c
  // IO 锁数组，对应了每个 Buffer
LWLockMinimallyPadded *BufferIOLWLockArray = NULL;
// 直接返回对应位置的IO锁
#define BufferDescriptorGetIOLock(bdesc)  (&(BufferIOLWLockArray[(bdesc)->buf_id]).lock)
```



## 总结

postgresql 对于缓存并发控制是非常精细的，它尽量将耗时的操作放在锁外面执行，很大的提高了并发性。还有进一步将不耗时的操作，通过乐观锁的方式实现。这些设计思想值得细细品味，对我们的程序设计会有很大帮助。

