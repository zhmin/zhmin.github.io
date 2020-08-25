---
title: Postgresql BRIN 索引原理
date: 2020-08-25 20:30:23
tags: postgresql, brin
categories: postgresql
---

## 前言

postgresql 提供了块级索引（简称 BRIN），主要适用于类似时序数据之类的，有着天然的顺序，而且都是添加写的场景。相比于 btree 索引，它的体积小得多，非常适用于大数据量的场景。



## 原理

postgresql 按照一定的数目（默认 128， 可以通过 pages_per_range 指定），将相邻的数据 Block 分成一组，然后计算它的的取值范围。当需要查看数据时，会先遍历这些取值范围。当要查找的数据不在此范围内，则可以直接跳过这些数据 Block。

<img src="pg-range-principle.svg">



当数据按照一定规则新增时，比如监控数据，数据的查找会非常高效。而且块级索引的空间占用会很小，多个相邻的Block才会对应一条索引记录。

如果数据排列的比较随机时，那么索引效果就非常差，因为它起不到快速筛除不符合的数据 Block。造成数据排列乱的原因，还有频繁的删除数据，因为 postgresql 会将删除空间回收掉，后续的数据新增都会填补这些空间。虽然可以配置删除的数据不会回收，但是会造成存储空间浪费，所以块级索引还不适合频繁删除数据的场景。



## 存储结构

BRIN 也是通过 Page 为基本单位来存储数据的，它有三种类型的 Page， 排列如下图所示：

<img src="pg-block-range-index.svg">



BRIN 的第一个 Page 是 Meta Page，它存储了整个索引的元信息。数据定义如下：

```c
typedef struct BrinMetaPageData
{
	uint32		brinMagic;   // 标识符
	uint32		brinVersion;  // 版本号
	BlockNumber pagesPerRange;  // 每个范围包含了多少个原始数据的page
	BlockNumber lastRevmapPage;  // 最大 revmap 的page number
} BrinMetaPageData;
```



紧接着 Meta Page 后面的是 Range Map Page，它相当于一个 ItemPointerData 数组，可以快速的根据源数据来查找到索引数据。如下图所示，每128个 block 会计算一次汇总信息：

<img src="pg-range-page.svg">



我们如果想要找到源数据块 block 238 对应的索引数据，首先计算出它对应第几组，即 238 / 128 = 1。然后每个 Range Map Page 包含的数量是一定的（记为 REVMAP_PAGE_MAXITEMS），最终计算出它对应第几个 Range Map Page， 即 1 / REVMAP_PAGE_MAXITEMS = 0，在这个 Page 的偏移量是 1 % REVMAP_PAGE_MAXITEMS = 1。

计算的公式如下：

```c
targetblk = HEAPBLK_TO_REVMAP_BLK(revmap->rm_pagesPerRange, heapBlk) + 1 // 这里加1是指 meta page

// 计算第几个Range Map Page, REVMAP_PAGE_MAXITEMS表示一个 Page 包含的 ItemPointData 数目
#define HEAPBLK_TO_REVMAP_BLK(pagesPerRange, heapBlk)    ((heapBlk / pagesPerRange) / REVMAP_PAGE_MAXITEMS)  

// 计算 Page 里的偏移量
#define HEAPBLK_TO_REVMAP_INDEX(pagesPerRange, heapBlk)    ((heapBlk / pagesPerRange) % REVMAP_PAGE_MAXITEMS) 
```



Range Map Page 存储的是指向`BrinTuple`的位置，注意到`BrinTuple`的存储并没有按照 block number 排序的 。`BrinTuple`存储在 Regular Page 里，它存储的数据格式如下：

<img src="pg-brin-tuple.svg">



bt_blkno 指向所属  BLock Range 的第一个 Block

bt_info 是一个 uint8 类型，第8位表示是否包含 null 值，1到4位表示 values 的偏移量。

all_nulls 表示值是否都为 null

has_nulls 表示值是否有null

values 表示索引字段的范围



## 数据更新

### 数据新增

当数据新增时，postgresql 并不一定会更新块级索引。如果开启了 autosummarize（默认为关闭，在创建索引可以指定），那么在填满了一个 Range Block（默认是 128 个），再触发一次新增的时候，才会发送 autosummarize 请求给 autovaccum 进程。还需要注意到，如果请求队列满了，该 autosummarize  请求会被丢弃的。除此之外，索引的更新只能通过 vaccum 或者用户主动调用 brin_summarize_new_values 函数执行。

如果数据新增是在已有的 block 上，比如postgresql 将之前被删除的数据回收掉，新的数据被添加进来了，那么这时就会更新索引。如果新增的数据，在原来的范围内，就不会做任何操作。

### 数据删除

当数据被删除时，postgresql 不会更新索引。因为这种操作非常耗时，如果删除的数据刚好在范围边界，那么需要扫描整个 Range Block，才能计算出来，所以 postgresql 会等到 vacuum 时才会处理，况且这种情况并不会影响结果的准确性，只是扫描了多余的数据。

### 数据更新

postgresql 更新数据，本质是先删除数据然后新增数据，所以对于索引的影响同上面一样。