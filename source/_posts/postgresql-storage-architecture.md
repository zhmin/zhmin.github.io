---
title: Postgresql 存储设计
date: 2020-09-05 18:11:05
tags: postgresql, storage
categories: postgresql
---



## 架构图

<img src="pg-storage-architecture.svg">



用户查询指定 page 的数据

1. 首先查询该 page 是否在缓存中，通过 hash table 快速查找它在缓存池的位置
2. 如果存在，那么从缓存池读取返回
3. 如果不存在需要从磁盘读取数据，并且放入到缓存池中，然后返回

## postgresql  存储单位

postgresql 底层存储的数据交互是以 Page 为单位的，它会负责将数据持久化到底层的文件系统里。postgresql 的这种设计应该是基于磁盘考虑的，因为我们大部分的存储介质都是磁盘，磁盘都是由固定大小的扇区组成，一般是512字节。文件系统负责与磁盘交互，它为了利用磁盘顺序读写的特点，将数据交互的单位设置为更大的块，一般是4KB。因为每种磁盘和文件系统的交互单位并不一定相等，postgresql 为了更好的跨平台性，将数据单位设置为 Page，默认 8KB。



## 数据读写安全性

虽然一些磁盘或者文件系统支持一定长度的原子读写，但是长度不一致，而且有些系统并没有实现。如果 postgresql 想支持跨平台，那么它不能依靠底层的原子操作，需要自身保证这些数据的正确写入，防止 part-write 问题（也就是当写入一个 Page 时机器断电了，可能造成 Page 只写了一部分）。

postgresql 为了解决整个问题，它使用了 xlog 存储了每次修改。并且在 page 第一次修改时，还会记录该 page 的全部数据。这样即使发送了 part-write 问题，也可以从 xlog 恢复出来，具体细节可以参考 postgresql checkpoint full-page write。



## Buffer Pool

如果每次读写数据都需要从磁盘操作，那么造成读取速度很慢，所以在实现数据库时，都会加上缓存。一般来说，数据库为了更加精细化的管理，不会采用文件系统的自身缓存，而是自身来实现缓存管理，因为数据库对数据更加理解。postgresql 使用了 Buffer Pool 来实现缓存。

当需要取数据时，需要先从 Buffer Pool 中查找，如果没有则需要从磁盘加载到缓存。因为 Buffer Pool 是对外层用户透明的，用户只是传输 Page 地址，为了快速的查找指定 Page 在 Buffer Pool 的位置，postgresql 使用了 hash table 来存储。



Buffer Pool 在 postgresql 的定义如下，它只是一个简单的数组

```c
char	   *BufferBlocks;            // 缓存数组
BufferDescPadded *BufferDescriptors;  // 缓存元数据

// 初始化缓存数组
void InitBufferPool(void)
{
    // 创建一块共享内存，里面存储了 NBuffers 个缓存，每个缓存的大小为 BLCKSZ
    BufferBlocks = (char *) ShmemInitStruct("Buffer Blocks", NBuffers * (Size) BLCKSZ, &foundBufs);
    // 创建一块共享内存，里面存储了 NBuffers 个缓存元数据
    BufferDescriptors = (BufferDescPadded *) ShmemInitStruct("Buffer Descriptors", NBuffers * sizeof(BufferDescPadded), &foundDescs);
}

```

Buffer 存储的数据是和 Page 完全一样的。我们还需要一些描述 Buffer 的信息，比如该 Buffer 对应哪个 Page，这里当持久化该 Buffer 的时候，知道存储到磁盘的哪个位置。还有为了支持并发，需要保存一些并发的信息。这些信息都由 BufferDesc 保存，需要强调下 BufferDesc 信息是不会被持久化的。

```c
typedef struct BufferDesc
{
	BufferTag	tag;			/* 指定对应的 page 地址 */
	int			buf_id;			/* 指定的 buffer 在数组中的索引 */

	/* state of the tag, containing flags, refcount and usagecount */
	pg_atomic_uint32 state;

	int			wait_backend_pid;	/* backend PID of pin-count waiter */
	int			freeNext;		/* link in freelist chain */

	LWLock		content_lock;	/* 锁，用于并发控制 */
} BufferDesc;
```

我们查看下 `BufferTag`的定义，看看 postgresql 是如何表示 page 地址的。postgresql 对于表是单独存储的，每个表除了包含了本身的数据文件，还有其它一些类型的文件。

```c
typedef struct buftag
{
	RelFileNode rnode;			/* 表标识 */
	ForkNumber	forkNum;         /* 文件类型 */
	BlockNumber blockNum;		/* page 在底层文件的索引 */
} BufferTag;
```



我们仔细看看`BufferDesc`虽然是数组，但是在创建的时候，却是以`BufferDescPadded`格式存储的。

```c
#define BUFFERDESC_PAD_TO_SIZE	(SIZEOF_VOID_P == 8 ? 64 : 1)

typedef union BufferDescPadded
{
	BufferDesc	bufferdesc;
	char		pad[BUFFERDESC_PAD_TO_SIZE];
} BufferDescPadded;
```

为什么需要在`BufferDesc`添加一个 64 bytes 的空闲数据呢。其实这是 postgresql 针对 cpu cache 做的优化，这里简单说下 cache 原理。如今的 cpu 处理速度越来越快，以至于内存的读取速度已经不能满足了，所以每个 cpu 都会有着独立的 高速 cache。当 cpu 要处理数据时，会先从内存中把数据加载到 cache，每次交互的数据单位大小叫做 cache line size，一般是 64 bytes。当两个 cpu 并行的处理同一块数据，那么就会造成该数据都会被拷贝到两个 cpu 的 cache 里。如果一个 cpu 修改了该 cache，那么会造成另外一个 cpu 的 cache 失效，那么另外的 cpu 就会重新去共享内存或者共享 cache 取数据，这种现象称为 false share。

postgresql 解决这个问题非常粗暴，在 BufferDesc 之间添加了 64bytes 的空闲数据，这样就能够保证在同一个 cache line 是不可能包含多个 `BufferDesc`，避免了 false share。不过这也造成了内存浪费，假如我们的缓存个数为 1024 个，那么就会造成 64Kb 的空间浪费。

