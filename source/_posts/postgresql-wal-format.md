---
title: Postgresql Wal 日志构建原理
date: 2019-11-05 16:02:51
tags: postgresql, storage
categories: postgresql
---

## 前言

Postgresql 使用 wal 日志保存每一次的数据修改，这样保证了数据库即使意外宕机，也能利用它准确的恢复数据。wal 日志也叫做 xlog，在 9.4 版本之后作了重大更新，本篇只讲解最新版的格式。wal 日志被用于多个方面，比如修改数据，修改索引等，每种用途的格式都不相同，但是构建方式是相同的，本篇会先介绍wal日志的相同部分，然后讲解构建的原理。



## 结构概览

一条 wal 日志分成四个部分：

1. Xlog 头部，存储着事务ID，日志长度，校检码，使用用途等信息
2. 块头部，描述了对应数据页的位置，是否包含数据页的内容，是否被压缩等
3. 块私有数据，这里的每个数据都隶属于一个数据页，由使用者自己定义
4. XLog 数据，由使用者自身定义

<img src="pg-xlog-format.svg">



## Xlog 头部

postgresql 在9.4版之前的wal 日志格式，由于每种用途的格式都完全不一样，并且也没有一个通用的标准，使得对于它的处理比较混乱，所以 postgresql 在9.4版本之后，统一了 wal 日志的头部。注意这里只是作了头部统一，数据区域的格式仍然不一致。通用头部的格式定义如下：

```c
typedef struct XLogRecord
{
	uint32		xl_tot_len;		/* 整个xlog的长度，包含头部 */
	TransactionId xl_xid;		/* xact id */
	XLogRecPtr	xl_prev;		/* ptr to previous record in log */
	uint8		xl_info;		/* 标记位 */
	RmgrId		xl_rmid;		/* 表示用于哪种用途 */
	pg_crc32c	xl_crc;			/* xlog的crc32c校检码，不包含头部 */
} XLogRecord;
```

`xl_info`标记位分为两部分。高四位使用者自己定义，低四位如下：

```c
#define XLR_SPECIAL_REL_UPDATE	0x01
#define XLR_CHECK_CONSISTENCY	0x02
```





## 备份模式

在介绍块头部之前，还需要介绍一下备份模式的 wal。一般情况下，wal 日志只会记录此次修改的数据。当数据库执行恢复操作时，会将根据磁盘文件和 wal 日志，就能完整的将数据恢复。这种方案成立的前提是磁盘文件必须是完好无损的，如果在 data buffer 写入文件的过程中刚好宕机，那么文件的数据就会不完整，也就是被损坏了。

postgresql 为了解决这个问题，它会在 checkpoint 之后，当数据block被第一次修改时，会在 xlog 中记录数据 block 的全部数据。这样数据库修复时，直接从上次 checkpoint 的位置开始恢复，这样就不用担心写过程中失败的问题。这种记录了数据block的 xlog，称为备份模式的 xlog。



## 块头部

一条 xlog 可能会涉及修改到多个数据 block，每个数据block都对应着一个头部，这里称作为块头部。块头部根据 xlog 是否为备份模式，数据 block 是否有空闲空间，数据 block在备份时是否被压缩，分为下列三种：

| 场景                                      | 格式                                                         |
| ----------------------------------------- | ------------------------------------------------------------ |
| 无备份模式                                | `XLogRecordBlockHeader ` + `RelFileNode` + `BlockNumber`     |
| 备份模式                                  | `XLogRecordBlockHeader ` + `XLogRecordBlockImageHeader` + `RelFileNode` + `BlockNumber` |
| 备份模式，开启压缩，数据 block 有空闲空间 | `XLogRecordBlockHeader ` + `XLogRecordBlockImageHeader` + `XLogRecordBlockCompressHeader` + `RelFileNode` + `BlockNumber` |



### XLogRecordBlockHeader  格式

`XLogRecordBlockHeader`的结构很简单

```c
typedef struct XLogRecordBlockHeader
{
	uint8		id;				/* 数组种的索引 */
	uint8		fork_flags;		/* 低4位是fork类型，高四位是标记位 */
	uint16		data_length;	/* rdata数据长度 */
} XLogRecordBlockHeader;
```

`fork_flags`高位标记位格式如下：

| 名称          | 含义                   |      |
| ------------- | ---------------------- | ---- |
| SAME_RELATION | 和上条记录属于同一张表 |      |
| WILL_INIT     |                        |      |
| HAS_DATA      | 是否有 rdata           |      |
| HAS_IMAGE     | 是否为备份模式         |      |



### XLogRecordBlockImageHeader 格式

我们直到数据 block 的存储格式，它的空间是从顶部和底部开始，向中间分配的方式，所以它的空闲空间是处于中间的连续空间，具体原理参见这篇博客。postgresql 在备份这个数据block，会忽略掉空闲空间。`XLogRecordBlockImageHeader `的定义如下：

```c
typedef struct XLogRecordBlockImageHeader
{
	uint16		length;			/* 如果开启压缩，表示压缩后的数据长度。没有开启压缩，表示忽略空闲数据后的长度 */
	uint16		hole_offset;	/* 空闲数据的起始位置 */
	uint8		bimg_info;		/* 标记位 */
} XLogRecordBlockImageHeader;
```

标记位格式

| 属性名                 | 含义                         |
| ---------------------- | ---------------------------- |
| BKPIMAGE_HAS_HOLE      | 是否有空闲数据               |
| BKPIMAGE_IS_COMPRESSED | 是否数据被压缩               |
| BKPIMAGE_APPLY         | 是否在恢复数据时使用此条日志 |



### XLogRecordBlockCompressHeader 格式

只有在开启了数据压缩，并且数据block有空闲空间，头部才会包含`XLogRecordBlockCompressHeader `。它的结构很简单：

```c
typedef struct XLogRecordBlockCompressHeader
{
	uint16		hole_length;	/* 空闲数据的长度 */
} XLogRecordBlockCompressHeader;
```



### 数据块位置字段

在每个块末尾，还有两个字段 rnode 和 blocknum 表示对应数据块的位置。rnode 为 `RelFileNode`类型，用来确定数据块属于哪张表，如果和上个数据block相同，那么可以省略。blocknum 为`BlockNumber`类型，表示数据块的编号。



## 头部剩余字段

头部末尾还有如下字段

| 名称                      | 类型                                               | 含义                                                         |
| ------------------------- | -------------------------------------------------- | ------------------------------------------------------------ |
| replorigin_type           | char                                               | 当为253时，会有replorigin_session_origin字段。否则忽略掉replorigin_session_origin字段。 |
| replorigin_session_origin | RepOriginId                                        |                                                              |
| mainrdata_len_type        | char                                               | 当mainrdata_len大于255，为254。否则为255。                   |
| mainrdata_len             | 如果值小于等于255，则为uint8类型。否则为uint32类型 | xlog 数据的长度                                              |



## 数据区域

上面介绍完头部格式，接下来介绍数据区域的格式。数据区域没有统一的格式，每种用途都有着自己的格式，postgresql 也只是将数据区域简单地划分为两部分，块私有数据区域和 xlog 数据区域。块私有数据依次按序存储，xlog 数据存储在最后面。



## 构建 WAL 日志

postgresql 提供了构建 wal 日志的一些列方法，调用过程如下

1. 调用`XLogBeginInsert`方法，表示开始构建 xlog
2. 调用`XLogRegisterBuffer`方法，添加数据页
3. 调用`XLogRegisterBufData`方法，将数据添加到块私有数据
4. 调用`XLogRegisterData`方法，将数据添加到 xlog 数据
5. 调用`XLogInsertRecord`方法，写入xlog 到文件中
6. 调用`XLogResetInsertion`方法，重置构建时用到的缓存

下面以插入单条数据为例，

```c
XLogBeginInsert();
XLogRegisterData((char *) &xlrec, SizeOfHeapInsert);    // xlog 数据的格式是xlrec
XLogRegisterBuffer(0, buffer, REGBUF_STANDARD | bufflags);  // 记录修改的数据block
XLogRegisterBufData(0, (char *) &xlhdr, SizeOfHeapHeader);  // 添加块私有数据xlhdr
XLogRegisterBufData(0, (char *) heaptup->t_data + SizeofHeapTupleHeader,
                    heaptup->t_len - SizeofHeapTupleHeader); // 添加数据block的私有数据heaptup
XLogSetRecordFlags(XLOG_INCLUDE_ORIGIN);  // 设置插入配置
XLogInsert(RM_HEAP_ID, info);  // 持久化 xlog
```





## 构建 WAL 日志原理

上面介绍了构建 wal 日志的方法，接下来讲解它们是如何实现的，这些方法都定义在`src/backend/access/transam/xloginsert.c`文件中。



### 数据链表节点

`XLogRecData`结构表示链表节点，每次添加块私有数据或者 xlog 数据，都会生成一个节点，添加到对应的链表中。如果时块私有数据，那么会添加到块的私有链表里。如果是共享数据，那么会添加到全局的共享链表里。

```c
typedef struct XLogRecData
{
	struct XLogRecData *next;	/* 指向下个节点，如果为NULL表示到达链表结尾 */
	char	   *data;			/* 数据的起始地址 */
	uint32		len;			/* 数据的长度 */
} XLogRecData;
```



### 块私有区域数据

当添加一个数据页到 wal 日志里时，会生成一个`registered_buffer`实例，里面包含了自身的私有数据链表。

```c
typedef struct
{
	bool		in_use;			/* is this slot in use? */
	uint8		flags;			/* REGBUF_* flags */
	RelFileNode rnode;			/* 指定所属表的存储目录 */
	ForkNumber	forkno;         /* 哪种文件类型 */
	BlockNumber block;          /* 块编号 */
	Page		page;			/* 对应的原始数据页 */
    
	uint32		rdata_len;		/* 私有数据链表的长度总和 */
	XLogRecData *rdata_head;	/* 私有数据链表头部节点 */
	XLogRecData *rdata_tail;	/* 私有数据链表尾部节点 */

	XLogRecData bkp_rdatas[2];	/* 存储着压缩后或忽略空闲数据的数据，如果有空闲位置且没有压缩，那么数据会被分成两个部分，存储在两个数组元素里 */

	char		compressed_page[PGLZ_MAX_BLCKSZ]; /* 如果开启了压缩，那么存储着压缩后的数据 */
} registered_buffer;
```



`flags`存储标记位格式：

```c
#define REGBUF_FORCE_IMAGE	0x01	/* force a full-page image */
#define REGBUF_NO_IMAGE		0x02	/* don't take a full-page image */
#define REGBUF_WILL_INIT	(0x04 | 0x02)	/* page will be re-initialized at
											 * replay (implies NO_IMAGE) */
#define REGBUF_STANDARD		0x08	/* page follows "standard" page layout,
									 * (data between pd_lower and pd_upper
									 * will be skipped) */
#define REGBUF_KEEP_DATA	0x10	/* include data even if a full-page image
									 * is taken */
```



### XLog 数据

Xlog 数据同样是链表的形式组织起来，由全局变量管理

```c
static XLogRecData *mainrdata_head; /* 链表头 */
static XLogRecData *mainrdata_last = (XLogRecData *) &mainrdata_head; /* 链表尾 */
static uint32 mainrdata_len;	/* 链表所有节点的数据总和 */
```



### 合并数据节点

当 wal 日志持久化到磁盘里时，会将链表节点的数据合并，然后存储到文件中。所以使用者设计存储格式时，需要考虑到读取时如何能够分割。



## 构建预分配

因为 wal 日志构建非常频繁，postgresql 为了减少内存申请和释放的开销，采用了预分配机制。



### registered_buffer 数组

当添加一个数据block，就需要创建出一个`registered_buffer`实例。postgresql 提前创建了一个`registered_buffer`数组，这样每次只需要从中取出空闲元素就行。



### XLogRecData 数组

当添加一个数据时，就需要创建出一个`XLogRecData`实例。postgresql 提前创建了一个`XLogRecData`数组，这样每次只需要从中取出空闲元素就行。



### 头部预分配

当构建一条 wal 日志时，就需要创建出一个头部。postgresql 提前创建了一个最大空间的头部，这样无论多大的 wal 日志，它的头部都能包含。下面来看看 postgresql 如何计算最大头部长度：

```c
#define SizeOfXlogOrigin	(sizeof(RepOriginId) + sizeof(char))
#define XLR_MAX_BLOCK_ID			32

#define HEADER_SCRATCH_SIZE \  
	(SizeOfXLogRecord + \    /* XLog 头部 */
	 MaxSizeOfXLogRecordBlockHeader * (XLR_MAX_BLOCK_ID + 1) + \  /* 块头部的最大长度 * 最大块数目 */
	 SizeOfXLogRecordDataHeaderLong + \   /* xlog 数据的长度信息 */
     SizeOfXlogOrigin)   /* RepOriginId 信息 */
```



块头部的最大长度计算：

```c
#define MaxSizeOfXLogRecordBlockHeader \
	(SizeOfXLogRecordBlockHeader + \             /* 块基本头部 */
	 SizeOfXLogRecordBlockImageHeader + \        /* 块备份模式 */
	 SizeOfXLogRecordBlockCompressHeader + \     /* 块压缩信息 */
	 sizeof(RelFileNode) + \                     /* 数据块的位置 */
	 sizeof(BlockNumber))
```