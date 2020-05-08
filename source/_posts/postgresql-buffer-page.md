---
title: Postgresql Page 结构
date: 2019-10-11 19:41:37
tags: postgresql, page
categories: postgresql
---

## 前言

postgresql 保存数据的基本单位是 page，一个 page 里包含多条数据。postgresql 同磁盘的读写单位也是 page，一个 page 对应于磁盘的一个 block。block 的格式和 page 是相同的，本篇文章详细得介绍了 page 的数据存储格式和相关的增删改查操作。



## 内存结构

page 可以简单划分为四块区域：

1. page 头部区域，描述整个 page 的情况，比如空闲空间，校检值等
2. 数据指针区域，数据指针用来描述实际数据的存储信息
3. 数据区域，用来存储实际数据
4. 特殊区域，用来存储一些特殊数据

<img src="pg-buffer-page-1.svg">

其中数据指针区域和数据区域是空间共享的，数据指针区域的区间是从上面开始的，向下扩展。而数据区域的空间方向是相反的，从下面开始的，向上扩展。

每条数据存储在 page 里，都对应一个数据指针和一个数据，数据指针记录了实际存储数据的位置。这种共享机制能够充分的利用空间，无论每条数据的是否过大或过小，都能几乎填满整个 page。



## Page 头部

page 头部由结构体`PageHeaderData`来表示，

```c
typedef struct PageHeaderData
{
	PageXLogRecPtr pd_lsn;	// 该数据页最后一次被修改对应的wal日志的位置
	uint16		pd_checksum;	// 校检值
	uint16		pd_flags;	// 标记位
	LocationIndex pd_lower;	// 空闲空间的起始偏移量
	LocationIndex pd_upper;	// 空闲空间的结束偏移量
	LocationIndex pd_special;	// 特殊空间的结束偏移量
	uint16		pd_pagesize_version;	// page 格式版本号
	TransactionId pd_prune_xid; /* oldest prunable XID, or zero if none */
	ItemIdData	pd_linp[FLEXIBLE_ARRAY_MEMBER]; // 数据指针数组
} PageHeaderData;
```

当新添加一条数据到 page 里，需要快速判断是否有空闲空间。page 的 pd_flags 记录了 page 是否有空闲空间，它的标记位如下：

```c
#define PD_HAS_FREE_LINES	0x0001	// 是否有空闲的数据指针
#define PD_PAGE_FULL		0x0002	// 是否有空闲空间支持添加一条数据
#define PD_ALL_VISIBLE		0x0004	/* all tuples on page are visible to everyone */
```



注意到 pd_linp 成员，它是一个 ItemIdData 数组，这里需要把它看成一个指针。它并不属于头部，从计算头部的长度就可以看出

```c
#define SizeOfPageHeaderData (offsetof(PageHeaderData, pd_linp))
```

整个结构如下图所示：

<img src="pg-buffer-page-2.svg">



## 数据指针

ItemData 结构表示数据的指针，它描述了数据的位置和状态

```c
typedef struct ItemIdData
{
	unsigned	lp_off:15,		// 数据在page的偏移量
				lp_flags:2,		// 状态值，有unused,normal,redirect,dead
				lp_len:15;		// 数据长度
} ItemIdData;
```

lp_flags 只有2位，它有四种状态值：

```c
#define LP_UNUSED		0		// 表示此指针是空闲
#define LP_NORMAL		1		// 表示此指针正在被使用，且对应的数据已经存储
#define LP_REDIRECT		2		// HOT redirect (should have lp_len=0) */
#define LP_DEAD			3		// dead, may or may not have storage */
```



## 插入数据

`PageAddItemExtended`函数负责插入数据，定义如下

```c
/*
 * 参数item是要写入的数据，参数size是数据的长度
 * 参数offsetNumber指定数据指针的位置，如果对于位置没有要求，值为InvalidOffsetNumber
 * 参数flags是标记位，可以指明数据类型是否是tuple类型，还可以指定是否覆盖已有数据
 */
OffsetNumber PageAddItemExtended(Page page, Item item, Size size, OffsetNumber offsetNumber, int flags);
```

如下图所示，原来的 page 有三条数据 data0、data2、data3，而 data 1 数据已经被删除，所以数据指针 ItemIdData 1 的位置是空闲的，现在要插入 data4 数据。

中间的图片展示了两种插入情况，没有指定数据指针的位置，和指定了数据指针位置为第2个（也就是原有的 ItemData 1 空闲位置）并且指定了覆盖选项。

后面的图片展示了指定数据指针的位置第三个（也就是为原有 ItemData2 的位置）。

<img src="pg-buffer-page-add.svg">

插入数据的原理总结如下：

1. 如果没有指定数据指针的位置，那么会尽量使用空闲位置的，通过检查 `PD_HAS_FREE_LINES` 标记位，就可以判断page 是否有空闲数据指针。如果有空闲位置，那么就从头开始遍历，直到找到一个空闲位置。如果没有前面空闲位置，只能使用 pd_lower 指向的位置。
2. 如果指定了数据指针位置，并且设置了覆盖选项，那么首先会去检查该位置的指针是否已经被使用，如果没有被使用则直接修改指针属性和插入数据，否则就会报错。
3. 如果指定了数据指针位置，但没有指定覆盖，那么不管此位置是否空闲，都需要将后面的指针后移一位。



## 删除数据

`PageIndexTupleDelete`负责删除指定位置的数据，删除数据后，会将需要将空闲的数据指针和数据进行压缩合并。

```c
void PageIndexTupleDelete(Page page, OffsetNumber offnum);
```

下图展示了数据删除的过程，这里需要删除数据 data 2：

<img src="pg-buffer-page-delete.svg">

首先将数据指针删除，然后将后面的数据向上移动，填满空缺位。

然后将实际的存储数据删除，将后面的数据向下移动，填补空缺位。

最后还需要更新数据指针的 offset 属性，因为其对应的数据存储位置已经发生了改变。



## 修改数据

```c
bool PageIndexTupleOverwrite(Page page, OffsetNumber offnum, Item newtup, Size newsize);
```

如果原有数据的大小和新数据相同，那么直接修改对应的数据指针和实际的数据。

如果不一致，需要先将数据进行删除，然后将删除的空间进行压缩合并，并且更新所有数据指针的 offset 属性。最后才完成添加数据。

