---
title: Postgresql Varlena 结构
date: 2020-08-30 01:23:28
tags: postgresql
categories: postgresql
---



## 前言

postgresql 会有一些变长的数据类型，存储都是采用 varlena 格式的（除了 cstring 类型），通过语句` SELECT typname FROM pg_type WHERE typlen = -1`就可以看到所有采用 varlena 格式的数据类型，比如常见的 text ，json 类型。



## varlena 结构

`varlena`结构有一个通用的定义格式，如下所示

```c
struct varlena
{
	char		vl_len_[4];		/* Do not touch this field directly! */
	char		vl_dat[FLEXIBLE_ARRAY_MEMBER];	/* Data content is here */
};
```

注意这是通用格式，`varlena`还分为很多种格式，每种格式的定义都不相同。我们在使用之前，需要根据它的第一个字节，转换为它对应格式：

1. 第一个字节等于`1000 0000`, 那么就是`varattrib_1b_e`，用来存储 external 数据（在 toast 会有讲解到）
2. 第一个字节的最高位等于1，然后字节不等于`1000 0000`，那么就是`varattrib_1b`，用来存储小数据
3. 第一个字节的最高位等于0，那么就是`varattrib_4b`，可以存储不超过1GB的数据



## varattrib_1b 类型

`varattrib_1b`类型的格式如下：

```c
typedef struct
{
	uint8		va_header;
	char		va_data[FLEXIBLE_ARRAY_MEMBER]; /* Data begins here */
} varattrib_1b;
```

`va_header `只有 8 位，最高位是标记位，值为 1。剩余的 7 位表示数据长度，所以`varattrib_1b`类型只是用于存储长度不超过127 byte 的小数据。

```shell
-----------------------------------------
   tag   |         length               |   
-----------------------------------------
  1 bit  |        7 bit                 |
-----------------------------------------
```



## varattrib_4b类型

`varattrib_4b`格式会根据存储的数据是否被压缩过分为两种。最高第二位为1，则表示存储的数据是未压缩的。为0，则表示存储的数据是压缩过的。`varattrib_4b`的定义如下，使用`union`来表示这两种情况。对于未压缩的数据，使用`va_4byte`结构体存储。对于压缩的数据，使用`va_compressed`结构体存储。

```c
typedef union
{
    /* va_data存储的数据没有被压缩 */
	struct						
	{
		uint32		va_header;
		char		va_data[FLEXIBLE_ARRAY_MEMBER];
	}			va_4byte;
    
    /* va_data存储的数据被压缩过了 */
	struct						
	{
		uint32		va_header;
		uint32		va_rawsize; /* 原始数据的大小 */
		char		va_data[FLEXIBLE_ARRAY_MEMBER]; 
	}			va_compressed;
    
} varattrib_4b;
```

这两个结构体的第一个成员`va_header`都是 uint32 类型，两者的格式是一样的。它最高位是标记位，值为 0。第二位表示是否没被压缩。剩下的 30 位表示数据的长度，所以只能支持不超过 1GB  (2^30 - 1 bytes) 的数据。

```shell
--------------------------------------------------
    tag    |    compress    |      length        |
--------------------------------------------------
   1 bit   |    1 bit       |      30 bit        |
-------------------------------------------------
```



## varattrib_1b_e 类型

它并不存储数据，只是指向了外部数据的地址。根据外部数据的存储位置，可以分为几种格式。首先看看它的定义：

```c
typedef struct
{
	uint8		va_header;		
	uint8		va_tag;			/* 类型 */
	char		va_data[FLEXIBLE_ARRAY_MEMBER];
} varattrib_1b_e;
```

第二个字节`va_tag`表示类型，有下面四种。每种类型下，它的 `va_data`存储的格式都不是一样的。

```c
typedef enum vartag_external
{
	VARTAG_INDIRECT = 1,
	VARTAG_EXPANDED_RO = 2,
	VARTAG_EXPANDED_RW = 3,
	VARTAG_ONDISK = 18
} vartag_external;
```



### 外部数据存储在磁盘 

如果是 `VARTAG_ONDISK`类型，它表示外部数据存储在磁盘中。  `va_data`存储的格式定义如下

```c
typedef struct varatt_external
{
	int32		va_rawsize;		/* Original data size (includes header) */
	int32		va_extsize;		/* External saved size (doesn't) */
	Oid			va_valueid;		/* Unique ID of value within TOAST table */
	Oid			va_toastrelid;	/* RelID of TOAST table containing it */
}			varatt_external;
```



### 外部数据存储在内存

如果外部数据是存储在内存中，则对应`VARTAG_EXPANDED_RO`和`VARTAG_EXPANDED_RW`类型。两者唯一的区别是前者只读，后者可以读写。

```c
typedef struct varatt_expanded
{
	ExpandedObjectHeader *eohptr;  // 指针
} varatt_expanded;
```



### 指针类型

还剩下一种特殊格式 `VARTAG_INDIRECT`，它只是一个`varlena`指针，可以指向`varatt_external`，`varatt_expanded`，或者是`varattrib_1b`，`varattrib_4b` 类型的原始数据。

```c
typedef struct varatt_indirect
{
	struct varlena *pointer;	/* Pointer to in-memory varlena */
}			varatt_indirect;
```



## 使用

postgresql 提供了很多宏（在`src/include/postgres.h`文件），方便我们操作 `varlena`数据。下面以`varattrib_4b`类型为例

| 宏                                 | 说明                                           |
| ---------------------------------- | ---------------------------------------------- |
| `VARDATA(PTR)`                     | 获取`varattrib_4b`结构体的数据起始地址         |
| `VARSIZE(PTR)`                     | 获取`varattrib_4b`结构体的长度                 |
| `SET_VARSIZE(PTR, len)`            | 设置`varattrib_4b`结构体头部，（标记位和长度） |
| `SET_VARSIZE_COMPRESSED(PTR, len)` | 设置`varattrib_4b`结构体的头部（标记位和长度） |



下面展示一个创建 `varlena`数据的例子

```c
result = (struct varlena *) palloc(length + VARHDRSZ); // 分配堆内存
SET_VARSIZE(result, length + VARHDRSZ);   // 设置头部
memcpy(VARDATA(result), mydata, length); // 写入数据
```





## 设计思想

postgresql 设计了 `varlena`结构，主要是为了解决`cstring`的问题。我们知道 `cstring`在获取长度时，只能从头扫描到结尾，才能知道，这种效率是非常低的。`varlena`为了支持不同大小的数据，还要避免空间的浪费，所以对于小型数据，将长度的信息和标记位都融合在了一个字节里。因为外部数据格式只是存储了一个指针，并且数据长度都可以确定了，所以头个字节不需要存储长度信息，使用`1000 0000`仅仅作为标记。其余的格式就都归纳给`varattrib_1b`类型了，并且数据长度肯定是大于 0 的，所以不会有冲突。

对于大型的数据，长度信息就需要占用比较多的位来表示，所以 postgresql 采用了四个字节来存储。为了避免浪费，它将头两个位作为标记位使用了。可以看到 postgresql 对于数据格式的设计是非常精妙的，我们可以多多学习。



