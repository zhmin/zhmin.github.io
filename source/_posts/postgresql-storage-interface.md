---
title: Postgresql 文件存储层
date: 2019-10-09 21:51:01
tags: postgresql, storage
categories: postgresql
---



## 前言

在 postgresql 数据库里，数据都会以表的形式组织起来。表的数据会被持久化到底层的磁盘里。负责与底层的磁盘交互，就是 postgresql 的文件存储层。本篇博客会依次介绍表的文件构成、分片机制和存储接口。



## 表的文件类型

postgresql 使用 `RelFileNode` 来标识一张表

```c
typedef struct RelFileNode
{
	Oid			spcNode;		/* tablespace */
	Oid			dbNode;			/* database */
	Oid			relNode;		/* relation */
} RelFileNode;
```

数据表都有下面三种文件

1. main 文件存储着实际的数据，
2. fsm 文件记录着 main 文件块的空闲大小，当有数据插入时，会优先从空闲位置写入。
3. vm 文件记录了过期的数据，这在 vaccum 清洗过程中有用到。

` ForkNumber` 枚举定义文件类型

```c
typedef enum ForkNumber
{
	InvalidForkNumber = -1,
	MAIN_FORKNUM = 0,  // 存储着实际的数据，文件名没有后缀
	FSM_FORKNUM,       // 存储着空闲位置，文件名的后缀为fsm
	VISIBILITYMAP_FORKNUM,  // 文件名的后缀为vm
	INIT_FORKNUM  // 用于数据库初始化，文件名的后缀为init
} ForkNumber;

#define MAX_FORKNUM		INIT_FORKNUM // 等于3
```



## 文件分片

因为有些文件系统对单个文件的数目大小有限制，postgresql 数据库为了良好的移植性，将上述文件进行切片，每个切片对应着一个文件，通过在文件名添加数字后缀来区分（注意第一个分片没有数字后缀）。每个文件切片由`MdfdVec`表示

```c
typedef struct _MdfdVec
{
	File		mdfd_vfd;		// 文件描述符
	BlockNumber mdfd_segno;		// 分片索引
} MdfdVec;
```



## 块存储

postgresql 存储数据并不是一条一条的存储，而是将数据合并成一个小块，每个块的大小都相同，默认为8KB。之所以这样设计，是因为磁盘的读写速度太慢了，尤其是随机读写，通过增加单次的吞吐量，来提高读写性能。

每个块都有一个唯一的标识，叫做 block number。它们依次递增，连续的存储在文件里。每个文件分片包含的块数目是相同的。下面展示了根据 block number 找到对应的文件切片，

```c
// blkno是块的唯一标识，RELSEG_SIZE是切片包含的块数目
targetseg = blkno / ((BlockNumber) RELSEG_SIZE);
```



## 动态哈希表

postgresql 使用 `SMgrRelationData`来表示单个表的文件结构，下面展示了主要成员

```c
typedef struct SMgrRelationData
{
	RelFileNodeBackend smgr_rnode;	// 表的标识

	BlockNumber smgr_targblock; // 准备写的blocknum
	BlockNumber smgr_fsm_nblocks;	// 最大的fsm文件的fsm block number，用于快速判断用户传递的blocknum是否超过最大值，需要扩充
	BlockNumber smgr_vm_nblocks;	/* last known size of vm fork */

	int			smgr_which;		// 选择哪个存储接口，目前只有一种实现，默认为0
    
	int			md_num_open_segs[MAX_FORKNUM + 1];  // 每种类型文件的分片数目
	struct _MdfdVec *md_seg_fds[MAX_FORKNUM + 1];  // 每种类型对应的分片数组
} SMgrRelationData;
```

注意到和分片相关的两个数组`md_num_open_segs`和`md_seg_fds`，它们的数组长度都是文件类型的数目。前者是一个一维数组，表示打开的分片文件的数目。后者是一个二维数组，存储着每种类型文件的的分片 。

这里还额外说下`RelFileNodeBackend`的定义，它与`RelFileNode`的区别仅仅是多了一个`BackendId`

```c
typedef struct RelFileNodeBackend
{
	RelFileNode node;  // 表标识
	BackendId	backend; // 属于后台哪个进程，值为-1表示普通表，否则表示临时表
} RelFileNodeBackend;
```



postgresql 为了方便的找到表对应的 `SMgrRelationData`，使用了动态哈希表存储。动态哈希表和普通的哈希表区别，在于可以动态的扩容而尽可能的减少数据在槽之间的移动。这个动态哈希表的 key 类型为 `RelFileNodeBackend`，value 类型为 `SMgrRelationData`。



## 存储接口

postgresql 使用一组函数指针组成的结构体，定义了存储接口。目前只有一种实现。如果用户想扩展，只需要实现这个接口就行。下面只取几个重要的接口：

```c
typedef struct f_smgr
{
	void		(*smgr_init) (void);	// 存储系统初始化
	void		(*smgr_shutdown) (void);  // 存储系统关闭
	void		(*smgr_open) (SMgrRelation reln); // 打开表
	void		(*smgr_close) (SMgrRelation reln, ForkNumber forknum);  // 关闭指定类型的文件 
	void		(*smgr_create) (SMgrRelation reln, ForkNumber forknum, bool isRedo); // 创建指定类型的文件
	void		(*smgr_extend) (SMgrRelation reln, ForkNumber forknum, BlockNumber blocknum, char *buffer, bool skipFsync);  // 新建数据块

	void		(*smgr_read) (SMgrRelation reln, ForkNumber forknum, BlockNumber blocknum, char *buffer);   // 读取块数据
	void		(*smgr_write) (SMgrRelation reln, ForkNumber forknum, BlockNumber blocknum, char *buffer, bool skipFsync); // 写入块数据
	BlockNumber (*smgr_nblocks) (SMgrRelation reln, ForkNumber forknum);  // 返回指定类型文件的块数目
	void		(*smgr_immedsync) (SMgrRelation reln, ForkNumber forknum); // 立即刷新数据到存储层
} f_smgr;
```

下面展示了用户如何使用这些接口

1. 调用 smgropen 创建 SMgrRelation 实例，这里并没有任何文件操作
2. 调用 smgrcreate 方法创建底层文件，如果底层文件之前创建过，那么此步可以跳过。
3. 调用 smgrread 方法读取数据
4. 调用 smgrwrite 方法写入数据



## 存储接口实现

下面的声明了实现接口的函数，这些函数的实现定义在`src/backend/storage/smgr/md.c`文件。

```c
static const f_smgr smgrsw[] = {
	/* magnetic disk */
	{
		.smgr_init = mdinit,
		.smgr_shutdown = NULL,
		.smgr_open = mdopen,
         // ......
	}
};
```

下面挑选出比较重要的函数来讲解，代码都是经过简化过的。

### 创建文件

`mdcreate`函数负责创建文件

```c
void mdcreate(SMgrRelation reln, ForkNumber forkNum, bool isRedo) {
    MdfdVec    *mdfd;
    char	   *path;
    File		fd;
    // 找到文件目录
    path = relpath(reln->smgr_rnode, forkNum);
    // 获取文件描述符
    fd = PathNameOpenFile(path, O_RDWR | O_CREAT | O_EXCL | PG_BINARY);
    // 设置此类型文件的分片数目为1，并且截断分片数组
    _fdvec_resize(reln, forkNum, 1);
    // 设置分片数组的第一个分片
    mdfd = &reln->md_seg_fds[forkNum][0];
    mdfd->mdfd_vfd = fd;
    mdfd->mdfd_segno = 0;
}
```



### 查找块位置

因为数据存储最终都是存储在文件分片中，所以对于数据块的读写，都必须先打开分片文件。`_mdfd_getseg`函数会根据 block number，找到对应的分片。如果分片已经打开了，则直接返回。如果没有，则需要先打开前面的分片文件，最后才打开指定的分片文件。

```c
static MdfdVec* _mdfd_getseg(SMgrRelation reln, ForkNumber forknum, BlockNumber blkno, bool skipFsync, int behavior);
```

一般来说，数据块都是按照顺序写入分片文件的，只有在这个分片写满后，才会创建新的分片。但是有时会出现异常，比如中间有块segment 并没有写满，那么这个时候就需要用户来抉择如何处理。用户可以通过指定 behavior 参数：

1. `EXTENSION_CREATE` 表示没有写满的部分，直接填充空的block。
2. `EXTENSION_CREATE_RECOVERY` 表示只有系统处于恢复模式下，回填充空的block。
3. `EXTENSION_RETURN_NULL` 表示遇到这种情况，直接返回 null。
4. `EXTENSION_FAIL` 表示直接报错。

### 块读取

然后再来看看数据块的读取操作，`mdread`函数会调用`_mdfd_getseg`函数打开文件分片，注意到这里的 behavior 参数设置为 `EXTENSION_FAIL` 和 `EXTENSION_CREATE_RECOVERY`。

```c
void mdread(SMgrRelation reln, ForkNumber forknum, BlockNumber blocknum, char *buffer)
{
    // 打开文件分片
    v = _mdfd_getseg(reln, forknum, blocknum, false, EXTENSION_FAIL | EXTENSION_CREATE_RECOVERY);
    // BLCKSZ表示数据块的长度，这里返回切片文件的偏移位置
    seekpos = (off_t) BLCKSZ * (blocknum % ((BlockNumber) RELSEG_SIZE));
    // 从文件中读取数据到参数 buffer 指定的位置
    nbytes = FileRead(v->mdfd_vfd, buffer, BLCKSZ, seekpos, WAIT_EVENT_DATA_FILE_READ);
    if (nbytes != BLCKSZ)
    {
        // 如果读取的块长度不等于标准值，需要额外处理
    }
}
```

### 块写入

再看看数据块的写入操作，`mdwrite`函数会调用`_mdfd_getseg`函数打开文件分片，注意到这里的 behavior 参数设置为 `EXTENSION_FAIL` 和 `EXTENSION_CREATE_RECOVERY`。

```c
void mdwrite(SMgrRelation reln, ForkNumber forknum, BlockNumber blocknum,
		char *buffer, bool skipFsync)
{
    
	v = _mdfd_getseg(reln, forknum, blocknum, skipFsync, EXTENSION_FAIL | EXTENSION_CREATE_RECOVERY);
    seekpos = (off_t) BLCKSZ * (blocknum % ((BlockNumber) RELSEG_SIZE));
    nbytes = FileWrite(v->mdfd_vfd, buffer, BLCKSZ, seekpos, WAIT_EVENT_DATA_FILE_WRITE);
    if (nbytes != BLCKSZ) {
        // 如果成功写入的长度不等于标准值，需要额外处理
    }
    
    // 如果指定skipFsync为false，并且不是临时表，那么需要进行fsync操作，要立即刷新到磁盘
    if (!skipFsync && !SmgrIsTemp(reln))
        register_dirty_segment(reln, forknum, v);
}
```



### 块新增

最后看看数据块的新增函数，由`mdextend`函数负责。它的代码和`mdwrite`是一样的，只不过调用`mdwrite`函数传递的behavior 不一样，它传递的是`EXTENSION_CREATE`，表示如果该对应的segment不存在，会自动创建该文件。





## 函数简介

下面简单的列表出一些常见的函数

```c
// 获取指定segment文件包含的block数目，这里MdfdVec代表着segment文件
static BlockNumber _mdnblocks(SMgrRelation reln, ForkNumber forknum, MdfdVec *seg);

// 打开segment文件，segno参数代表着segment的编号，
// oflags可以指定打开标志，支持o_create，o_rdwr，o_binary等
// 如果文件不存在并且没有指定o_create，那么返回 null
static MdfdVec* _mdfd_openseg(SMgrRelation reln, ForkNumber forknum, BlockNumber segno, int oflags);

// 根据segment编号，获取对应的文件路径
static char* _mdfd_segpath(SMgrRelation reln, ForkNumber forknum, BlockNumber segno);

// 获取指定类型的所有文件，包含的block数目总和，
// 因为每个segment文件都是按照顺序存储，并且最大block数目都是固定的。
// 前面的segment存储满后才会创建新的segment，所以计算方法为：最后一个segment的block数目 + 前面的segment文件数目 * segment文件包含的最大block数目
BlockNumber mdnblocks(SMgrRelation reln, ForkNumber forknum);


// 向指定的block写入数据，注意到这个block必须事先存在
// 参数buffer指向数据，参数skipFsync表示是否不需要sync操作，也就是立即持久化到磁盘
void mdwrite(SMgrRelation reln, ForkNumber forknum, BlockNumber blocknum, char *buffer, bool skipFsync);

// 标识segment文件的更新数据还没被持久化，这里首先会发出通知给checkpointer进程，如果发送失败，则会自己调用fsync操作完成
static void register_dirty_segment(SMgrRelation reln, ForkNumber forknum, MdfdVec *seg);


// 打开指定类型的第一个segment文件，如果文件不存在，behavior指定了EXTENSION_RETURN_NULL则返回null
// 其余情况的behavior灰报错
static MdfdVec* mdopenfork(SMgrRelation reln, ForkNumber forknum, int behavior);
```

