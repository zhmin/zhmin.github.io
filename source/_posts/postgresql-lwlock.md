---
title: Postgresql LWLock 原理
date: 2019-10-15 20:59:32
tags: postgresql, lwlock
categories: postgresql
---



## 前言

postgresql 的架构是基于多进程的，进程之间往往存在着数据共享，比如缓存读写。这时 postgresql 需要提高一种锁机制来支持并发访问，但是 linux 只实现了线程间的读写锁，所以 postgresql 自己实现进程间的读写锁，它主要利用了信号量和自旋锁来实现。



## 进程结构

每个进程都对应一个独立 `PGPROC` 实例，它包含了多个成员，下面只列出与锁相关的

```c
struct PGPROC 
{
    BackendId	backendId;		/* This backend's backend ID (if assigned) */
    PGSemaphore sem;			// 信号量

    // 正在等待的lwLock
    bool		lwWaiting;		// 是否在等待lwlock
    uint8		lwWaitMode;		// lwlock加锁类型，有读锁和写锁
    proclist_node lwWaitLink;	// 位于等待列表中的位置
}
```

`PGPROC` 实例存储在进程共享内存里，在创建进程时会被初始化。这里需要提醒下，信号量 sem 的初始值为0，属于无名信号量。



## 锁结构

`LWLock`结构体表示读写锁，结构如下：

```c
typedef struct LWLock
{
	uint16		tranche;		// tranche id，表示用于哪个方面
	pg_atomic_uint32 state;		// 状态值，包含多个标记位
	proclist_head waiters;		/* 等待进程链表 */
} LWLock;
```

需要介绍下 state 字段的标记位

```c
#define LW_VAL_SHARED         1                    // 1~23位表示读锁的数量
#define LW_VAL_EXCLUSIVE     ((uint32) 1 << 24)    // 是否写锁已经被占用
#define LW_FLAG_LOCKED       ((uint32) 1 << 28)    // 锁标记位，用来保护进程列表的并发操作
#define LW_FLAG_HAS_WAITERS  ((uint32) 1 << 30)    // 是否有进程在等待，用于快速判断等待列表是否为空
#define LW_FLAG_RELEASE_OK   ((uint32) 1 << 29)    // 是否可以执行唤醒操作（目前我还没弄懂为什么需要这个标记位）
```



## 加锁原理

`LWLockAcquire`函数负责整个加过程，这里将过程分为下面四步：

1. 尝试获取锁，如果获取成功，会直接返回，否则执行第二步。
2. 将自身进程添加到等待队列后，还会进行一次尝试获取锁，因为有可能在刚加入队列之前，锁恰好被释放。如果获取锁成功，则将自身从队列删除并且直接返回，否则执行第三步。
3. 通过信号量阻塞，等待其他进程唤醒。
4. 当因为锁释放被唤醒之后（该进程已经被唤醒进程从等待队列里删除了），会回到第一步

从上面可以看出来，这里采用的是非公平锁机制，也就是谁的速度快，谁就能获取到锁，没有先来先获取的顺序。



### 尝试获取锁

```c
static bool LWLockAttemptLock(LWLock *lock, LWLockMode mode);
```

尝试通过 CAS 操作，设置`LW_VAL_EXCLUSIVE`或`LW_VAL_SHARED`标记位。来获取锁。使用到的 CAS 操作函数如下：

```c
// 如果当前ptr指向的值，等于expected指向的值，那么就会更改ptr指向的值为newval。
// 无论是否成功修改，当前ptr的最新值会保存到 expected。
// 返回true表示修改成功，返回false表示修改失败
bool pg_atomic_compare_exchange_u32(volatile pg_atomic_uint32 *ptr, uint32 *expected, uint32 newval);
```

当尝试获取写锁时，需要检查`EXCLUSIVE`和`SHARED`标记位，只有两者为零，才能成功获取。

当尝试获取读锁时，只需要检查`EXCLUSIVE`标记位。如果为零，就认为成功获取。



### 添加到等待队列

对于队列的操作，都需要使用自旋锁来设置`LW_FLAG_LOCKED `标记位，才有权利操作队列。当设置成功后，会将自身添加到等待列表，然后更新自身进程对应`PGPROC`实例的 `lwWaiting` 和 `lwWaitMode` 成员，最后释放锁，清除标记位。

```c
static void LWLockQueueSelf(LWLock *lock, LWLockMode mode);
```

当加入到队列后，还需要更新`LW_FLAG_HAS_WAITERS`标记位，表示有进程在等待。



### 等待信号量

postgresql 是多进程架构的，进程之间使用信号量来同步。当进程获取锁失败时，会将自身添加到等待队列里，然后通过信号量进行阻塞，直到被别的进程唤醒。对于 postgresql 的每个进程都有一个信号量，由`PGPROC`实例的 sem 成员表示。sem 信号量的初始值为0，它的数值代表着满足的条件数。

因为这个 sem 信号量会有多个用途，所以每次唤醒操作，并不一定是期望的条件发生，所以进程在被唤醒之后，需要一次条件检查。如果是因为其他条件唤醒，还需要记录被唤醒次数，在事后需要将执行相应次数的唤醒操作。

```c
// 获取锁之前
extraWaits = 0
for (;;)
{
    // 执行信号量阻塞
    PGSemaphoreLock(proc->sem);
    // 当被唤醒之后，需要检查唤醒条件是不是因为锁释放，
    // 如果是锁释放的原因，proc->lwWaiting 会为false
    if (!proc->lwWaiting)
        break;
    // 如果不是期望条件，需要记录次数
    extraWaits++;
}

// 获取到锁之后
// 因为刚刚占有了其余条件的唤醒，所以现在需要进行补偿
while (extraWaits-- > 0)
        PGSemaphoreUnlock(proc->sem);
```

假设下面有一个进程A对应信号量A，它会被用于两个方面，条件A和条件B。

```shell
----------                          ------------                          ------------
 进程A    |    阻塞条件A ---->      |  信号量A   |   <----- 因为条件A唤醒   |   进程B   |
          |    阻塞条件B ---->      |           |   <----- 因为条件B唤醒   |           |
----------                          ------------                          ------------
```

进程A正在阻塞条件A，此时进程B因为条件B唤醒了进程A。但进程A发现并不是期望的条件所唤醒的，所以它会继续阻塞，直到满足条件A被唤醒，最后进程A还会执行一次唤醒操作。

### 离开等待队列

当获取到锁之后，如果自身在等待队列中，需要将其删除掉。如果之后队列为空，需要清除`LW_FLAG_HAS_WAITERS`标记位，当然这些操作都需要获取`LW_FLAG_LOCKED`锁。

### 源码

下面展示了`LWLockAcquire`的源码，描述了整个加锁过程

```c
bool LWLockAcquire(LWLock *lock, LWLockMode mode);
{
    bool result = true;
    int	extraWaits = 0;
    for (;;)
    {
        // 尝试获取锁，如果成功则直接返回
        bool mustwait = LWLockAttemptLock(lock, mode);
        if (!mustwait)
            break;
        
        // 步骤A，获取锁失败，则将自身添加到等待队列里
        LWLockQueueSelf(lock, mode);
    	// 步骤C
        
        // 在添加到队列时，有可能锁刚好释放。因为这时还未添加到队列里，所以不会通知它。
        // 所以这里需要再次尝试获取锁
        mustwait = LWLockAttemptLock(lock, mode);
    
        // 如果刚好成功获取了别人释放的锁，那么需要将自身从等待队列中删除
        if (!mustwait)
        {
            LWLockDequeueSelf(lock);
            break;
        }
    
        // 等待信号量，因为这个信号量是共享的，所以此次唤醒有可能是别的通知，而不是此次锁释放的原因
        // 通过检查 proc->lwWaiting 就可以判断是否是因为锁释放的原因唤醒的
        for (;;)
        {
            PGSemaphoreLock(proc->sem);
            if (!proc->lwWaiting)
                break;
            extraWaits++;
        }
    
        /* Retrying, allow LWLockRelease to release waiters again. */
        pg_atomic_fetch_or_u32(&lock->state, LW_FLAG_RELEASE_OK);
        // result为false表示需要将
        result = false;
    }
    // 将锁添加到队列里
    held_lwlocks[num_held_lwlocks].lock = lock;
    held_lwlocks[num_held_lwlocks++].mode = mode;
    
    // 因为刚刚有别的原因造成唤醒，所以现在需要唤醒其他等待进程相同次数
    while (extraWaits-- > 0)
        PGSemaphoreUnlock(proc->sem);
    
    return result;
}
```



## 释放锁

`LWLockRelease`函数负责释放锁，定义如下：

```c
void LWLockRelease(LWLock *lock);
```

释放锁分为下列步骤：

1. 清除锁标记位，如果之前占用的是写锁，那么清除`LW_VAL_EXCLUSIVE`标记位，如果是读锁，那么将读锁数量减1。
2. 检查`LW_FLAG_HAS_WAITERS`和`LW_FLAG_RELEASE_OK`标记位，如果都设置了并且现在读写锁都没有被占用，那么需要执行唤醒操作。
3. 从等待队列里按照 FIFO 的顺序，按照读锁优先，写锁互斥的原则，取出等待进程。然后将这些进程对应`PGPROC`的 `lwWaiting`成员设置为 false，并且通过成员`sem`信号量来唤醒。

下面列举了两种情况来阐述锁释放的原理：

1. 假设进程等待队列为`readlock0, readlock1, writelock1, readlock2`，这时释放了写锁。按照 FIFO 顺序，首先会释放读锁`readlock0`，然后会将后面的所有读锁都唤醒，包括`readlock1`和`readlock2`。
2. 假设进程等待队列为`writelock0, readlock2, readlock3, readlock4`，正在占用的读锁`readlock0, readlock1`。目前读锁`readlock0`释放，但由于`readlock1`还被占用，所以不会触发唤醒。当`readlock1`释放时，才会触发唤醒。按照写锁互斥的原则，只有`writelock0`会被唤醒，之后的读锁不会被唤醒。

在取出进程之后，会清除`LW_FLAG_RELEASE_OK`标记位。如果检查到队列为空，还会清除`LW_FLAG_HAS_WAITERS`标记位。



## LWLock 分类

lwlock 在 postgresql 中使用的场景非常多，为了更好管理这些 lwlock，postgresql 按照用途将它们分类，大体分为三个部分：

1. 系统场景，定义在`src/backend/storage/lmgr/lwlocknames.txt`文件里
2. 内置场景，比如buffer，wal等，由`BuiltinTrancheIds`表示，定义在`src/include/storage/lwlock.h`文件
3. 被用于插件场景，由`RequestNamedLWLockTranche `表示