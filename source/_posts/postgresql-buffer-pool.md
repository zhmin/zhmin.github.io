---
title: Postgresql 缓存池原理
date: 2019-10-17 21:02:16
tags: postgresql, buffer
categories: postgresql
---

## 前言

postgresql 为了提高响应速度，在存储层上面添加了一层缓存，这样对于数据的查询和修改，都会先在缓存中操作，后面才会同步到磁盘。这样对于一些热点数据，性能提高非常明显。缓存的基本单位对应文件的一个 block，本篇主要介绍缓存池的原理和使用。



## 缓存结构

缓存分为共享缓存和本地缓存，共享缓存会被所有进程访问，一般普通的表和索引都会使用它。而本地缓存只有自身进程可以访问到，仅仅只有临时表使用。

无论是共享缓存还是本地缓存，postgresql 都是使用一个固定大小的 buffer 数组来存储。当 buffer 不够用时会使用算法剔除一些不常用的元素。 



## Buffer 结构体

BufferTag 用来表示缓存的内容是哪个文件的block

```c
typedef struct buftag
{
	RelFileNode rnode;			// 用来标识哪个表
	ForkNumber	forkNum;        // 表下面的文件类型
	BlockNumber blockNum;		// 对应的block编号
} BufferTag;
```

`BufferDesc` 包含了`buffer`的头部信息

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

这里说一下 `buf_id`，小于0表示本地缓存，大于0表示全局缓存。通过它可以计算出它在数组的位置。

`state` 字段是一个32位的数值，它的格式如下：

```shell
--------------------+------------------+-------------------
     10 bit         |     4 bit        |      18 bit      |
--------------------+------------------+-------------------
      标记位         |    累积访问次数   |     引用次数      |
--------------------+------------------+-------------------
```

它的高位是一个 10 位的标记位，其中每个标记位都有着不同的含义

```c
#define BM_LOCKED				(1U << 22)	// 锁标记位，用来保护state字段
#define BM_DIRTY				(1U << 23)	// 是否为脏页，也就是缓存被修改了，但对应的磁盘数据没被修改
#define BM_VALID				(1U << 24)	// 数据是否有效
#define BM_TAG_VALID			(1U << 25)	// tag 是否有效
#define BM_IO_IN_PROGRESS		(1U << 26)	// 是否正在进行磁盘io
#define BM_IO_ERROR				(1U << 27)	// 是否磁盘io发送错误
#define BM_JUST_DIRTIED			(1U << 28)	/* dirtied since write started */
#define BM_PIN_COUNT_WAITER		(1U << 29)	/* have waiter for sole pin */
#define BM_CHECKPOINT_NEEDED	(1U << 30)	/* must write for checkpoint */
#define BM_PERMANENT			(1U << 31)	/* permanent buffer (not unlogged, or init fork) */
```



## 全局缓存

### 缓存数组

全局缓存由一些 buffer 数组表示，这些数组相同索引的元素，对应同一个buffer。

```c
BufferDescPadded *BufferDescriptors;  // buffer头部数组
char	   *BufferBlocks;    // buffer数据数组
```

当我们知道 buffer id 后，就可以在数组中找到对应的 buffer，索引号等于 buffer id。



### 缓存数组初始化

全局缓存存储在共享内存里，共享内存是 linux 进程间通信的一种方式，在 postgresql 中通过可以调用`ShmemInitStruct`函数获取。下面展示了各种数组的初始化，数组大小由`NBuffers`指定，默认为1000，

```c
void InitBufferPool(void)
{
	/* Align descriptors to a cacheline boundary. */
	BufferDescriptors = (BufferDescPadded *) ShmemInitStruct("Buffer Descriptors",
						NBuffers * sizeof(BufferDescPadded),
						&foundDescs);

	BufferBlocks = (char *)	ShmemInitStruct("Buffer Blocks",
						NBuffers * (Size) BLCKSZ, &foundBufs);

	/* Align lwlocks to cacheline boundary */
	BufferIOLWLockArray = (LWLockMinimallyPadded *)	ShmemInitStruct("Buffer IO Locks",
						NBuffers * (Size) sizeof(LWLockMinimallyPadded),
						&foundIOLocks);
}
```



### 缓存哈希表

postgresql 会经常根据文件的 block 来查找对应的缓存，为了提高查找查找速度，这里使用了动态哈希表存储，如下所示：

```c
// 哈希表，key为BufferTag，value为buffer_id
static HTAB *SharedBufHash;

typedef struct
{
    BufferTag	key;			// 表示文件的哪个block
    int			id;				// buffer id
} BufferLookupEnt;
```

用户首先根据指定文件的哪个block，由 BufferTag 参数指定。然后在`SharedBufHash`哈希表中，找到对应的 buffer id。再根据 buffer id 计算出数组的索引，然后从数组中取出 buffer 头部和数据。



## 本地缓存

### 缓存数组

```c
BufferDesc *LocalBufferDescriptors = NULL;
Block	   *LocalBufferBlockPointers = NULL;
int32	   *LocalRefCount = NULL;
```

buffer id 和 数组索引的对应关系为`index = -(buffer_id + 2)`。



### 缓存数组初始化

本地缓存数组因为是进程私有，所以这里直接使用`calloc`分配内存。

```c
LocalBufferDescriptors = (BufferDesc *) calloc(nbufs, sizeof(BufferDesc));
LocalBufferBlockPointers = (Block *) calloc(nbufs, sizeof(Block));
LocalRefCount = (int32 *) calloc(nbufs, sizeof(int32));
```



### 缓存哈希表

本地缓存同样提供了哈希表，来提高查找速度，原理同全局缓存一样。



## 缓存读取

全局缓存和本地缓存的读取的原理大致相同，不过因为全局缓存是进程共享，而本地缓存属于进程私有，所以在分配和寻找缓存会有些区别，而且在修改全局缓存的时候需要锁来保护竞争。下面先介绍相同的部分，对于差异部分分开讲解。

### 读取配置

因为缓存的空间是有限的，当空间不够用时，会将旧的缓存替换掉。对于不同场景的读取，替换算法也是不一样的，关于算法原理，后面会有文章单独介绍。

读取方式

```c
typedef enum
{
	RBM_NORMAL,					/* 一般情况的读 */
	RBM_ZERO_AND_LOCK,			/* 分配一个buffer并且获取写锁返回，不需要从磁盘读取数据初始化 */
	RBM_ZERO_AND_CLEANUP_LOCK,	/* Like RBM_ZERO_AND_LOCK, but locks the page in "cleanup" mode */
	RBM_ZERO_ON_ERROR,			/* Read, but return an all-zeros page on error */
	RBM_NORMAL_NO_LOG			/* Don't log page as invalid during WAL replay; otherwise same as RBM_NORMAL */
} ReadBufferMode;
```

`ReadBuffer`函数负责读取表的数据，定义如下：

```c
Buffer ReadBuffer(Relation reln, BlockNumber blockNum)
{
	return ReadBufferExtended(reln, MAIN_FORKNUM, blockNum, RBM_NORMAL, NULL);
}
```

在介绍表的文件结构中，我们知道数据是存储在 main 类型文件里，所以传递的参数是`MAIN_FORKNUM`。它的读取方式为`RBM_NORMAL`表示正常读取，`BufferAccessStrategy`参数为 null 表示如果缓存满了，采用默认时钟清除算法来替换掉旧的缓存。

### 读取流程

1. 首先判断读取的表是不是临时表，如果是则表示需要从本地缓存中寻找，否则需要从全局缓存中寻找。
2. 从对应的缓存哈希表中寻找，如果哈希表存在则直接返回，如果不存在则需要分配一个缓存空间。
3. 根据读取方式，如果是`RBM_NORMAL`则从文件中读取数据到缓存，否则将缓存置零。



## 全局缓存锁

在进一步介绍介绍全局缓存的读取之前，需要先了解下锁的使用情况。

### 缓存头部锁

在修改缓存头部时，都需要获取它的头部锁。头部锁由`BM_LOCKED`标记位表示，有两种使用方法。一种是采用自旋锁实现。

`LockBufHdr`函数实现了头部锁，它使用了自旋锁和原子操作来实现

```c
uint32 LockBufHdr(BufferDesc *desc)
{
	SpinDelayStatus delayStatus;
	uint32		old_buf_state;
    // 初始化自旋锁
	init_local_spin_delay(&delayStatus);
	while (true)
	{
		// 原子性的修改BM_LOCKED标记位，并且返回修改前的值
		old_buf_state = pg_atomic_fetch_or_u32(&desc->state, BM_LOCKED);
		// 如果之前的BM_LOCKED标记位为0，被此次修改为1，认为获取锁成功
		if (!(old_buf_state & BM_LOCKED))
			break;
        // 可能进行一小段时间的睡眠
		perform_spin_delay(&delayStatus);
	}
	finish_spin_delay(&delayStatus);
    // 返回状态值
	return old_buf_state | BM_LOCKED;
}
```

使用方法如下：

```c
BufferDesc * buf; // 缓存头部
uint32 buf_state = LockBufHdr(buf); // 加锁
buf_state &= BM_TAG_VALID // 修改头部
UnlockBufHdr(buf, buf_state); // 设置状态，并且释放锁
```



还有一种是调用`WaitBufHdrUnlocked`函数自旋等待，然后使用 CAS 操作获取锁。下面的以设置`DIRTY`为例

```c
void MarkBufferDirty(Buffer buffer)
{
    // 获取对应的buffer头部
    bufHdr = GetBufferDescriptor(buffer - 1);
    // 获取state属性
    old_buf_state = pg_atomic_read_u32(&bufHdr->state);
    // 循环等待锁释放，在获取锁成功后，会设置dirty位
    for (;;)
	{
        // 如果锁被别人占用，那么等待锁的释放
        if (old_buf_state & BM_LOCKED)
            old_buf_state = WaitBufHdrUnlocked(bufHdr);
        // 设置标记位
        buf_state = old_buf_state;
        buf_state |= BM_DIRTY | BM_JUST_DIRTIED;
        // 使用cas操作来原子性的设置标记位，如果成功则退出，如果失败则重试
        if (pg_atomic_compare_exchange_u32(&bufHdr->state, &old_buf_state, buf_state))
            break;
    }
}
```



### 缓存读写锁

每个缓存都有一个读写锁在保护，通过 buffer id 就可以获取。使用方法如下：

```c
#define BufferDescriptorGetContentLock(bdesc)   ((LWLock*) (&(bdesc)->content_lock))
BufferDesc buffer = aaa;
LWLockAcquire(LWLock *lock, LWLockMode mode);

LWLockRelease(LWLock *lock);
```



### 缓存 IO 锁

下面展示了与缓存io操作相关的全局变量

```c
LWLockMinimallyPadded *BufferIOLWLockArray; // LWLock 数组，索引同buffer数组一致
static BufferDesc *InProgressBuf = NULL;  // 正在进行io的buffer，postgresql同时只能操作一个buffer进行io操作
static bool IsForInput; // 正在进行io操作的类型，表示是从磁盘读取数据到缓存，还是从缓存写入数据到磁盘
```

下面展示了缓存io锁的相关操作

```c
// 阻塞等待 IO_IN_PROGRESS 标记位清除
static void WaitIO(BufferDesc *buf);

// 执行io操作，参数forInput为true，表示从磁盘读取到缓存。参数为false，表示将缓存写入到磁盘
// 返回值为false表示其他进程已经完成io操作，返回true表示成功设置了io标记位，需要自己完成io操作
static bool StartBufferIO(BufferDesc *buf, bool forInput);

// 清除IO_IN_PROGRESS和IO_ERROR标记位，并且设置InProgressBuf为null
// 参数 clear_dirty 如果为true，它会清除DIRTY标记位（前提是没有设置JUST_DIRTIED标记位）
// 并且设置全局变量InProgressBuf为null，并且释放lw锁
static void TerminateBufferIO(BufferDesc *buf, bool clear_dirty, uint32 set_flag_bits);

// 中断io操作，这里会设置IO_ERROR标记位
void AbortBufferIO(void);

```

下面以将缓存写到磁盘为例，展示了io锁的用法

```c
static void FlushBuffer(BufferDesc *buf, SMgrRelation reln)
{
    // 获取io对应的lw锁
    StartBufferIO(buf, false);
    // 清除JUST_DIRTIED标记位
    buf_state = LockBufHdr(buf);
    buf_state &= ~BM_JUST_DIRTIED;
	UnlockBufHdr(buf, buf_state);
    
    // 如果设置了PERMANENT标记位，那么需要写入xlog文件
    if (buf_state & BM_PERMANENT)
		XLogFlush(recptr);
    // 从buffer block数组中找到数据，并且拷贝数据
    bufBlock = BufHdrGetBlock(buf);
    bufToWrite = PageSetChecksumCopy((Page) bufBlock, buf->tag.blockNum);
    // 写入磁盘
    smgrwrite(reln, buf->tag.forkNum, buf->tag.blockNum, bufToWrite, false);
    // 清除io标记位和dirty标记位
    TerminateBufferIO(buf, true, 0);
}

```



## 全部缓存读取

`ReadBuffer_common`函数负责处理全局缓存和本地缓存，

```c
static Buffer ReadBuffer_common(SMgrRelation smgr, char relpersistence, ForkNumber forkNum,
				  BlockNumber blockNum, ReadBufferMode mode,
				  BufferAccessStrategy strategy, bool *hit);

```



下面将它的代码做简化，只负责出路全局缓存，并且省略了异常处理。

```c
// 如果参数blockNum等于特殊值P_NEW，表示需要新增数据块
isExtend = (blockNum == P_NEW);
if (isExtend)
    // 获取新的blockNum
    blockNum = smgrnblocks(smgr, forkNum);
// 在缓存中查找是否有对应的数据
bool found;
bufHdr = BufferAlloc(smgr, relpersistence, forkNum, blockNum, strategy, &found);
if (found)
{
    if (!isExtend)
    {
        // 在缓存中找到了，根据ReadBufferMode参数，执行不同操作
        if (mode == RBM_ZERO_AND_LOCK)
            // 获取写锁
            LWLockAcquire(BufferDescriptorGetContentLock(bufHdr), LW_EXCLUSIVE);
        if (mode == RBM_ZERO_AND_CLEANUP_LOCK)
            // 获取写锁，准备删除
            LockBufferForCleanup(BufferDescriptorGetBuffer(bufHdr));
        return BufferDescriptorGetBuffer(bufHdr);
    }
    // 这种情况表示我们本来创建新的数据块，但是已经存在缓存中，并且valid标记位为1。
    // 这种情况属于异常情况，需要清除valid标记位，并且执行io操作。
    do
    {
        uint32 buf_state = LockBufHdr(bufHdr);
        Assert(buf_state & BM_VALID);
        buf_state &= ~BM_VALID;
        UnlockBufHdr(bufHdr, buf_state);
    } while (!StartBufferIO(bufHdr, true));
}

// 找到缓存数据位置
bufBlock = BufHdrGetBlock(bufHdr);
if (isExtend)
{
    // 将缓存置零，并且写入底层文件的新增数据块
    MemSet((char *) bufBlock, 0, BLCKSZ);
    smgrextend(smgr, forkNum, blockNum, (char *) bufBlock, false);
}
else
{
    if (mode == RBM_ZERO_AND_LOCK || mode == RBM_ZERO_AND_CLEANUP_LOCK)
        // 将缓存置零
        MemSet((char *) bufBlock, 0, BLCKSZ);
    else
        // 从文件中读取数据到缓存
    	smgrread(smgr, forkNum, blockNum, (char *) bufBlock);
}
if ((mode == RBM_ZERO_AND_LOCK || mode == RBM_ZERO_AND_CLEANUP_LOCK))
{
    // 获取写锁
    LWLockAcquire(BufferDescriptorGetContentLock(bufHdr), LW_EXCLUSIVE);
}
// 结束io操作，这里会设置valid标记位
TerminateBufferIO(bufHdr, false, BM_VALID);
// 返回buffer
return BufferDescriptorGetBuffer(bufHdr);

```



## 本地缓存读取

本地缓存的读取非常简单，因为不涉及到任何的锁。下面同样将`ReadBuffer_common`函数代码进行简化

```c
isExtend = (blockNum == P_NEW);
if (isExtend)
    blockNum = smgrnblocks(smgr, forkNum);
bufHdr = LocalBufferAlloc(smgr, forkNum, blockNum, &found);
if (found)
{
    if (!isExtend)
        return BufferDescriptorGetBuffer(bufHdr);
    // 清除valid标记位    
    uint32		buf_state = pg_atomic_read_u32(&bufHdr->state);
    buf_state &= ~BM_VALID;
    pg_atomic_unlocked_write_u32(&bufHdr->state, buf_state);
}
bufBlock = LocalBufHdrGetBlock(bufHdr)
if (isExtend)
{
    // 将缓存置零，并且写入底层文件的新增数据块
    MemSet((char *) bufBlock, 0, BLCKSZ);
    smgrextend(smgr, forkNum, blockNum, (char *) bufBlock, false);
}
else
{
    if (mode == RBM_ZERO_AND_LOCK || mode == RBM_ZERO_AND_CLEANUP_LOCK)
        // 将缓存置零
        MemSet((char *) bufBlock, 0, BLCKSZ);
    else
        // 从文件中读取数据到缓存
    	smgrread(smgr, forkNum, blockNum, (char *) bufBlock);
}
// 设置valid标记位
uint32		buf_state = pg_atomic_read_u32(&bufHdr->state);
buf_state |= BM_VALID;
pg_atomic_unlocked_write_u32(&bufHdr->state, buf_state);
// 返回buffer
return BufferDescriptorGetBuffer(bufHdr);

```