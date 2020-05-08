---
title: Postgresql 编写自定义 C 函数
date: 2019-12-17 22:18:17
tags: postgresql, function
categories: postgresql
---

## 前言

postgresql 支持自定义函数，并且还支持多种语言进行编写， 极大的提高了可扩展性。postgresql 支持使用 pgSQL(postgresql提供的类sql语言)，python 和 c 语言来编写函数，本篇主要讲解 c 语言，因为它的性能是最好的。



## 示例

编写 c 函数分为三部分：

1. 编写 c 文件，定义函数实现
2. 编译程序，需要编译成共享库
3. 在 postgresql 中执行 `CREATE FUNCTION`注册函数

下面展示一个简单的函数，用于两数相加。通过这个简单的例子，来看看操作流程。

### 编写函数

创建一个`my_add_func.c` 文件，内容如下

```c
#include "postgres.h"
#include "fmgr.h"

PG_MODULE_MAGIC;

PG_FUNCTION_INFO_V1(my_add_func);
Datum my_add_func(PG_FUNCTION_ARGS)
{
    int32 a = PG_GETARG_INT32(0);
    int32 b = PG_GETARG_INT32(1);
    int64 result = a + b;
    PG_RETURN_INT64(result);
}
```

这里面的函数很简单，只不过调用了一些莫名其妙的宏，所以会觉得疑惑。这些宏的原理在后面会讲到，这里先简单的介绍下作用。

`PG_MODULE_MAGIC`宏必须要使用，后面编译生成的库才可以被 postgresql 加载。

`PG_FUNCTION_INFO_V1`宏声明了函数的版本，这个也必须要使用，目前postgresql 只支持 v1 版本的函数。

`PG_FUNCTION_ARGS`宏定义了参数类型和名称，等于`FunctionCallInfo fcinfo`，这个函数名称会在取参数值会用到。

`PG_GETARG_INT32`宏表示取出指定位置的参数，并且转换为 int32 类型。

`PG_RETURN_INT64`宏表示将 int64 类型的数值，转换为`Datum`并且返回。



### 编译函数

上述的 c 文件，包含了 postgresql 里的头文件，所以在编译的时候需要指定头文件的位置。通过`pg_config --includedir-server`命令就可以找到。我是通过 yum 在 Centos 上装的 postgresql，执行情况如下：

```shell
[root@host1]# /usr/pgsql-9.6/bin/pg_config --includedir-server
/usr/pgsql-9.6/include/server
```

然后执行编译命令，记住这个生成共享库（so 结尾的文件）的路径，在创建函数时会用到

```shell
gcc -fPIC -c my_add_func.c -I/usr/pgsql-9.6/include/server/  # 生成my_add_func.o文件
gcc -shared -o my_add_func.so my_add_func.o            # 生成共享库
```



### 创建函数

然后在 postgresql 命令行执行`CREATE FUNCTION`命令

```sql
CREATE FUNCTION my_add(a integer, b integer) RETURNS integer
     AS '/var/lib/pgsql/my_add_func.so', 'my_add_func' LANGUAGE C STRICT;
```

接下来我们尝试使用 my_add 函数

```shell
test=# select my_add(1, 2);
 my_add 
--------
      3
(1 row)

test=# select my_add(id, price), id, price from orders ;
 my_add | id | price 
--------+----+-------
    124 |  1 |   123
(1 row)
```



## 数据类型

通过上面的例子，可以看到创建一个 c 函数并不难，不过还有些细节需要注意到，就是数据是如何在 postgresql 和函数之间传递的。

postgresql 支持的数据类型有多种，基本类型 int，char，double 等，还有复杂类型，比如字符串，结构体等。这些数据传递时都是由`Datum`类型表示，也就是指针类型。指针长度根据平台不同而不一样，有32位或64位。那么它是如何能够表示这么多类型的数据呢。下面分为两种情形来分析，基本数据类型和复杂数据类型。



## 基本数据传递

基本数据类型包含 int8，int16，int32，int64，float等数值型。因为`Datum`的长度根据系统的不同，可能是 4个字节 或 8个字节。如果基本类型的长度小于等于 `Datum`，那么将它的值直接存储在`Datum`。如果大于 `Datum`，就需要分配堆内存，然后将内存地址存储在`Datum`。

我们以 int16 类型的数值 -1 为例，它的二进制位`1111 1111 1111 1111`。这里假设`Datum`的长度为 4 个字节，将其转换为`Datum`之后的二进制位`0000 0000 0000 0000 1111 1111 1111 1111`。这里只是在高位填充0。当从`Datum`转换 int16 类型时，直接取对应的低位。这里转换规则和我们平时接触的不太一样，在 c 语言中如果是负数，会在高位填充1，正数才会填充0，目前还不太清楚原因。

再来看看 int64 数据类型，`Int64GetDatum`定义了转换规则。如果`Datum`的长度等于 8 个字节，那么就定义了`USE_FLOAT8_BYVAL`宏，也就是直接存储到`Datum`里。如果`Datum`的长度小于 8 个字节，那么则分配堆内存，然后将内存地址存储在`Datum`。

```c
#ifdef USE_FLOAT8_BYVAL
#define Int64GetDatum(X) ((Datum) (X))
#else
extern Datum Int64GetDatum(int64 X);
#endif

Datum Int64GetDatum(int64 X)
{
    // 使用palloc分配堆内存，palloc的作用同malloc一样，在它基础之上增加了内存管理
	int64	   *retval = (int64 *) palloc(sizeof(int64));
    // 设置堆内存值
	*retval = X;
    // 将内存地址存储在Datum
	return PointerGetDatum(retval);
}
```



### 相关宏

这里再来回顾下`PG_FUNCTION_ARGS`宏，它定义函数参数名称`fcinfo`，类型为`FunctionCallInfo`。`FunctionCallInfo`是一个结构体，里面有个`args`成员，表示参数数组。

```c
#define PG_FUNCTION_ARGS	FunctionCallInfo fcinfo
```

postgresql 为我们提供了提取参数的方法，下面列举其中一些

```c
// 提取指定位置的参数
#define PG_GETARG_DATUM(n)	 (fcinfo->args[n].value)

// 将指定位置的参数转换为uint16
#define PG_GETARG_UINT16(n)  DatumGetUInt16(PG_GETARG_DATUM(n))

// 将指定位置的参数转换为int32
#define PG_GETARG_INT32(n)	 DatumGetInt32(PG_GETARG_DATUM(n))
```



同样 postgresql 也为我们提供了返回结果的方法，

```c
// 将int32转换为Datum类型，并且返回
#define PG_RETURN_INT32(x)	 return Int32GetDatum(x)
// 将uint32转换为Datum类型，并且返回
#define PG_RETURN_UINT32(x)  return UInt32GetDatum(x)
```





## 复合数据传递

上述介绍的`my_add_func`例子，只是接收和返回基本类型。postgresql 还可以接收行数据，或者自定义的数据类型（比如postgis插件的  POINT 类型），这种情况叫做复合类型数据，它在 c 语言中 由 tuple 表示，下面演示一个实例：

c 文件内容

```c
#include "postgres.h"
#include "funcapi.h"

PG_MODULE_MAGIC;

PG_FUNCTION_INFO_V1(my_tuple_func);
Datum my_tuple_func(PG_FUNCTION_ARGS)
{
    // 第一个参数为复合类型
	HeapTupleHeader tuple = PG_GETARG_HEAPTUPLEHEADER(0);
	
	// 取出返回数据的类型，由TupleDesc表示
	TupleDesc	tupdesc;
	if (get_call_result_type(fcinfo, NULL, &tupdesc) != TYPEFUNC_COMPOSITE)
		elog(ERROR, "return type must be a row type");

	// 构建返回值
	Datum values[2];
	values[0] = PointerGetDatum(cstring_to_text("hello"));
	values[1] = Int32GetDatum(1);
    // nulls 数组表示值是否为空
	bool		nulls[2];
	memset(nulls, 0, sizeof(nulls));
	// 构建返回数据，由HeapTuple表示
	HeapTuple result = heap_form_tuple(tupdesc, values, nulls);
	PG_RETURN_DATUM(HeapTupleGetDatum(result));
}
```



创建函数语句如下，定义了接收参数类型为 record， 返回结果有两列（text，int）

```sql
CREATE FUNCTION my_tuple_func(tuple record) 
     RETURNS TABLE (message text ,num int)
AS '/var/lib/pgsql/my_tuple_func', 'my_tuple_func'
LANGUAGE C STRICT;
```



执行语句，返回的数据为复合类型，可以通过`.*`符号展示所有列

```shell
test=# select my_tuple_func(orders) from orders where id = 1;
 my_tuple_func 
---------------
 (hello,1)
(1 row)

test=# select (my_tuple_func(orders)).* from orders where id = 1; 
 message | num 
---------+-----
 hello   |   1
```



通过上面的例子，可以看到可以接收行数据，参数为对应的表名即可。它由`HeapTuple`类表示，关于`HeapTuple`的原理，可以参考后续的文件。

在获取参数时，我们又看到了一个新的宏`PG_GETARG_HEAPTUPLEHEADER`，它负责提取参数并且转换为`HeapTuple`类。

在构建返回结果时，我们需要调用`get_call_result_type`函数获取返回结果的类型，也就是`TupleDesc`。返回结果的类型是在我们执行`CREATE FUNCTION`时候创建的，通过`RETURNS TABLE `语句定义的。postgresql 在调用函数时，会将结果类型保存到参数`FunctionCallInfo`里。

还需要注意一点，上述函数返回的第一列是`TEXT`类型，它并不等于 C 语言的字符串，所以需要使用`cstring_to_text`函数转换。



## 返回多行数据

下面展示一个函数，用于生成指定数量的 [斐波那契数列](https://zh.wikipedia.org/zh-hk/斐波那契数列)。每行数据包括两列（num int, value int）。

c 语言文件

```c
#include "postgres.h"
#include "funcapi.h"

PG_MODULE_MAGIC;

// 计算斐波那契数列，需要记录前两个结果的值
typedef struct OlderValues {
    uint64 value1;
    uint64 value2;
} OlderValues;

PG_FUNCTION_INFO_V1(my_records_func);
Datum my_records_func(PG_FUNCTION_ARGS)
{
    FuncCallContext     *funcctx;

    // 判断是否第一次执行
    if (SRF_IS_FIRSTCALL())
    {
        // 进行此次函数第一次初始化
        funcctx = SRF_FIRSTCALL_INIT();
        // 切换内存分配器
        MemoryContext oldcontext = MemoryContextSwitchTo(funcctx->multi_call_memory_ctx);
        // 提取参数，保存到FuncCallContext的max_calls属性里
        funcctx->max_calls = PG_GETARG_UINT32(0);
        // 分配自定义上下文变量
        OlderValues * mydata = palloc(sizeof(OlderValues));
        mydata->value1 = 0;
        mydata->value2 = 1;
        funcctx->user_fctx = mydata;
        //切回内存分配器
        MemoryContextSwitchTo(oldcontext);
    }

    // 获取最新的FuncCallContext
    funcctx = SRF_PERCALL_SETUP();
    uint64 call_cntr = funcctx->call_cntr;
    uint64 max_calls = funcctx->max_calls;

    if (call_cntr < max_calls)
    {
        TupleDesc            tupdesc;
        if (get_call_result_type(fcinfo, NULL, &tupdesc) != TYPEFUNC_COMPOSITE)
            elog(ERROR, "return type must be a row type");

        // 构建返回值，使用调用次数作为第一列的值
        Datum values[2];
        values[0] = UInt64GetDatum(call_cntr);
        // 计算此次斐波那契的值
        uint64 value;
        if (call_cntr == 0) {
            value = 0;
        }
        if (call_cntr == 1) {
            value = 1;
        }
        if (call_cntr > 1) {
            // 取出上下文的变量，也就是前两个斐波那契的值
            OlderValues *mydata = (OlderValues *) funcctx->user_fctx;
            value = mydata->value1 + mydata->value2;
            mydata->value1 = mydata->value2;
            mydata->value2 = value;
        }
        values[1] = UInt64GetDatum(value);

        // nulls 数组表示值是否为空
        bool            nulls[2];
        memset(nulls, 0, sizeof(nulls));
        // 构建返回数据，由HeapTuple表示
        HeapTuple tuple = heap_form_tuple(tupdesc, values, nulls);
        Datum result = HeapTupleGetDatum(tuple);
        // 返回数据
        SRF_RETURN_NEXT(funcctx, result);
    }

    // 数据已经全部返回
    SRF_RETURN_DONE(funcctx);
}
```



创建函数

```sql
CREATE FUNCTION my_records_func(number_limit int) 
     RETURNS TABLE (num int ,value int)
AS '/var/lib/pgsql/my_records_func.so', 'my_records_func'
LANGUAGE C STRICT;
```



执行语句

```shell
test=# select * from my_records_func(5);
 num | value 
-------+-----
     0 |   0
     1 |   1
     2 |   1
     3 |   2
     4 |   3
(5 rows)
```



### 函数调用上下文

因为要返回多行数据，所以会执行函数多次。这里需要一个上下文变量，来保存需要在执行多次之间传递的变量，比如执行的次数等。`FuncCallContext`结构体，作为函数调用的内置上下文变量，下面只是列举常见的成员

```c
typedef struct FuncCallContext
{
	uint64		call_cntr;      // 已经返回结果的次数
    void	   *user_fctx;      // 用于保存自定义上下文
	MemoryContext multi_call_memory_ctx;   // 用于分配内存
    // .......
} FuncCallContext;
```



### 相关宏

上述的程序调用了多个宏，接下来简单介绍下它们的用途：

`SRF_IS_FIRSTCALL`负责判断是否这是第一次执行。

`SRF_FIRSTCALL_INIT`负责初始化`FuncCallContext`，它会设置`call_cntr`为 0，然后在`fn_mcxt`内存管理器下，分配一个子管理器，负责这个函数执行时的内存申请。使用内存管理器有一个好处，它会记录所有的申请内存，在所有的行数据返回完之后，会释放掉执行期间申请的所有内存。

`SRF_PERCALL_SETUP`负责返回最新的`FuncCallContext`。

`SRF_RETURN_NEXT`负责返回数据，会更新`call_cntr`自增，设置`isDone`为`ExprMultipleResult`，表示还有更多的行数据。

`SRF_RETURN_NEXT_NULL`负责返回空数据，执行过程同`SRF_RETURN_NEXT`相同。

`SRF_RETURN_DONE`负责释放函数执行期间申请的所有内存，并且设置`isDone`为`ExprEndResult`，表示所有的行数据已经返回完了。

