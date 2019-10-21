---
title: Postgresql 缓存替换算法
date: 2019-10-21 20:38:37
tags: postgresql, buffer
categories: postgresql
---

## 前言

postgresql 使用缓存来作为与磁盘读写的中间层，但是缓存的大小是有限的，当缓存不够用时需要剔除一些不常用的。但是如何高效的选出那些需要剔除的缓存呢，postgresql 针对不同的应用场景，提供了简单高效的不同算法。



## 替换算法

postgresql 主要提供了两种替换算法，ring 算法和 clock sweep 算法，分别针对批量读写和正常读写。

### ring 算法

ring算法适合那些批量读写的场景，因为操作数据都是一次性的，这些数据在使用后就没有作用了。如果频繁的触发缓存替换，对性能造成没必要的损耗的。所以使用一个特定大小的环，循环使用。这种算法性能高，而且对缓存造成很大的影响。

ring算法的原理很简单，它有一个环状数组，数组的每个元素都对应一个buffer，注意到buffer的分配是动态的，也就是说开始的时候，数组的元素都为空。

每次需要获取替换的缓存，它都会从上次遍历的位置开始，直接返回下一个元素。如果这个数组元素没有分配buffer，则需要进行分配。当这个buffer被分配了，它会一直存在，不会被替换掉，除非被明确释放掉。

ring 算法的相关参数定义如下：

```c
typedef struct BufferAccessStrategyData
{
	
	BufferAccessStrategyType btype;          // 策略类型
	int			ring_size;                  // 环大小	
	int			current;                    // 数组的遍历当前位置
	bool		current_was_in_ring;        // 数组当前位置的元素，有没有分配buffer

    // 这里使用数组来表示环，当元素的值为InvalidBuffer(也就是0)，表示还分配buffer给这个元素
	Buffer		buffers[FLEXIBLE_ARRAY_MEMBER];
} BufferAccessStrategyData;
```



上面的`BufferAccessStrategyType`定义了哪种策略，定义如下

| BufferAccessStrategyType | 使用场景           | 替换算法                                     |
| ------------------------ | ------------------ | -------------------------------------------- |
| BAS_NORMAL               | 一般情况的随机读写 | clock sweep 算法                             |
| BAS_BULKREAD             | 批量读             | ring算法，环大小为 256 * 1024 / BLCKSZ       |
| BAS_BULKWRITE            | 批量写             | ring算法，环大小为 16 * 1024 * 1024 / BLCKSZ |
| BAS_VACUUM               | VACUUM 进程        | ring算法，环大小为 256 * 1024 / BLCKSZ       |





### clock sweep 算法

clock sweep 算法根据访问次数来判断哪些数据为热点数据。当缓存空间不足，它会优先替换掉访问次数低的缓存。它的算法参数如下：

```c
typedef struct
{
	slock_t		buffer_strategy_lock;      // 自旋锁，用来保护下面的成员
	pg_atomic_uint32 nextVictimBuffer;     // 下次遍历位置

	int			firstFreeBuffer;	     // 空闲buffer链表的头部
	int			lastFreeBuffer;          // 空闲buffer链表的尾部

	uint32		completePasses; // 记录遍历完数组的次数
	pg_atomic_uint32 numBufferAllocs;	/* Buffers allocated since last reset */

	/*
	 * Bgworker process to be notified upon activity or -1 if none. See
	 * StrategyNotifyBgWriter.
	 */
	int			bgwprocno;
} BufferStrategyControl;
```

每次从上次位置开始轮询，然后检查buffer 的引用次数 refcount 和访问次数 usagecount。

1. 如果 refcount，usagecount 都为零，那么直接返回。
2. 如果 refcount 为零，usagecount 不为零，那么将其usagecount 减1，遍历下一个buffer。

3. 如果 refcount 不为零，则遍历下一个。


clock sweep 算法是一个死循环算法，直到找出一个 refcount，usagecount 都为零的buffer。



### 空闲链表

为了加快查找空闲 buffer 的速度，postgresql 使用链表来保存这些buffer。链表的头部和尾部由 BufferStrategyControl 结构体的 firstFreeBuffer 和 lastFreeBuffer 成员指定。链表节点由 BufferDesc 结构体表示，它的 freeNext 成员指向下一个节点。

当有新增的空闲buffer，它会被添加到链表的尾部。当需要空闲空间时，则直接返回链表的头部。



## 寻找空闲全局缓存

1. 如果指定使用了ring算法，那么首先从环中获取空闲缓存，如果找到则直接返回。
2. 查看空闲链表是否有空闲缓存，如果有则直接返回
3. 使用 clock sweep 算法查找到一个空闲缓存，如果之前指定了ring算法，需要将这个空闲位置添加到环中。
4. 如果缓存为脏页，那么需要将它刷新到磁盘。
5. 修改缓存的头部，并且更新缓存哈希表





## 寻找空闲本地缓存

寻找本地空闲缓存很简单，直接使用 clock sweep 算法寻找。在找到替换的缓存后，还需要处理脏页和修改缓存头部，并且更新缓存哈希表。





## 引用计数缓存

当获取到一个缓存时，不想让它被替换掉，那么就需要增加它的引用次数。替换算法只会替换那些引用次数为零的缓存，当被使用完后，需要减少它的引用次数，当减少至零就可以被替换掉。

本来state字段包含引用次数和使用次数，但是每次更新都需要获取头部锁。因为头部锁还被用于其他多个地方，所以为了减少冲突，单独使用了计数器存储了引用次数。现在 state 字段的引用次数只有0和1两个值，用来表示是否有被进程引用。

### 引用计数器

每个buffer都对应一个计数器，定义如下：

```c
typedef struct PrivateRefCountEntry
{
	Buffer		buffer;  // 类型为int，等于buffer_id
	int32		refcount;  // 引用计数器
} PrivateRefCountEntry;
```

为了快速找到指定buffer的引用计数，postgresql 提供了一个数组作为一级缓存，使用哈希表作为二级缓存。这个数组的长度为8，每个元素占有 8byte，选择数字8，刚好等于64byte，这个长度与CPU的缓存大小刚好相关。

```c
#define REFCOUNT_ARRAY_ENTRIES 8
static struct PrivateRefCountEntry PrivateRefCountArray[REFCOUNT_ARRAY_ENTRIES];  // 数组
static PrivateRefCountEntry * ReservedRefCountEntry = NULL;   // 指向数组中空余的位置


static HTAB *PrivateRefCountHash = NULL;  // 哈希表，key为buffer_id，value为引用次数
static int32 PrivateRefCountOverflowed = 0;  // 哈希表包含entry的数目
```



### 使用示例

下面列举了常用的函数：

```c
// 寻找顺组中的空闲位置，如果没有找到，那么就将数组挑选出一个，将其转移到哈希表中
// 挑选策略采用了轮询
static void ReservePrivateRefCountEntry(void);

// 将buffer添加到数组空闲位置
static PrivateRefCountEntry* NewPrivateRefCountEntry(Buffer buffer);

// 寻找指定buffer对应的PrivateRefCountEntry，
// 参数do_move为true时，表示当数据在哈希表里，需要将其转移到数组
static PrivateRefCountEntry* GetPrivateRefCountEntry(Buffer buffer, bool do_move);

// 返回引用次数，这里调用了GetPrivateRefCountEntry方法
static inline int32 GetPrivateRefCount(Buffer buffer);

// 释放PrivateRefCountEntry，通过比较数组的内存起始位置和结束位置，可以很快判断出是否在数组中
static void ForgetPrivateRefCountEntry(PrivateRefCountEntry *ref);
```



下面以新添加一个buffer的计数器为例：

```c
Buffer buffer;  // 指定buffer
ReservePrivateRefCountEntry();   // 保证数组有一个空闲位置
ref = NewPrivateRefCountEntry(buffer);  // 为buffer创建计数器
ref->refcount = 1; // 设置引用次数
```



## 修改引用次数

在增加或减少buffer的引用次数时，会涉及到 buffer头部的 state 字段，和引用计数的更新。

1. 当 buffer 的引用次数由0变为1时，会触发 state 字段的更新和创建新的计数器，之后的增加操作，就只会更改引用计数。
2. 当buffer 的引用次数由1变为0时，会触发state 字段的更新，并且会删除对应的计数器。如果不是减为0，那么只需要更改引用计数。



### 增加引用次数

修改引用次数的函数如下：

```c
// 增加引用次数，参数buf为指定的buffer，参数strategy指定了替换算法
static bool PinBuffer(BufferDesc *buf, BufferAccessStrategy strategy);

```



### 减少引用次数

在减少引用次数，先在引用计数器减少1，如果引用次数变为0，那么需要减少state字段的计数器，并且

```c
// 减少引用次数
static void UnpinBuffer(BufferDesc *buf, bool fixOwner);
```
