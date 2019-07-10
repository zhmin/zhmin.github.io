---
title: Spark Sql 基础类 TreeNode
date: 2019-06-16 21:03:50
tags: spark-sql
categories: spark sql
---



## 前言

Spark Sql 不仅支持 sql 语句，而且还会对 sql 进行自动优化。整体流程如下图所示：

<img src="Catalyst-Optimizer-diagram.png">



1. 接收 sql 语句，初步解析成 logical plan
2. 分析上步生成的 logical plan，生成验证后的 logical plan
3. 对分析过后的 logical plan，进行优化
4. 对优化过后的 logical plan，生成 physical plan
5. 根据 physical plan，生成 rdd 的程序，并且提交运行

这里的 logical plan 代表着 sql 语句的一部分，比如子类 Project 代表着选中的列，UnresolvedRelation 表示未验证的表名或者试图。

## 查看spark sql 的解析和优化过程

假设我们有两张表，fruit 表记录了各种水果的信息，orders 表记录了购买记录

```sql
 
 CREATE TABLE fruit (
     id INT, 
     name STRING, 
     price FLOAT, 
 );
 
 INSERT INTO fruit VALUES(1, "apple", 2.5);
 INSERT INTO fruit VALUES(2, "pear", 3.5);
 INSERT INTO fruit VALUES(3, "banana", 4.5);
 
 CREATE TABLE orders (
 	id INT,
    fruit_id INT,
    create_time TIMESTAMP,
    consumer_name STRING
 );
 
 INSERT INTO orders VALUES(1, 1, "2019-06-14 10:00:00", "consumer_1");
 INSERT INTO orders VALUES(2, 1, "2019-06-14 10:00:00", "consumer_1");
 INSERT INTO orders VALUES(3, 2, "2019-06-14 14:00:00", "consumer_2");
 
  -- 查找 每种水果的销量
  SELECT fruit.name, t1.num FROM fruit INNER JOIN (SELECT fruit_id, COUNT(*) num FROM orders GROUP BY fruit_id) t1 ON fruit.id = t1.fruit_id;
```



通过 spark2-shell 命令启动，它会自动创建一个变量名为spark 的 SparkSession实例。

```shell
scala> val sql = "SELECT fruit.name, t1.num FROM fruit INNER JOIN (SELECT fruit_id, COUNT(*) num FROM orders GROUP BY fruit_id) t1 ON fruit.id = t1.fruit_id" # 注意这里的sql语句不要接分号
scala> val logical = spark.sessionState.sqlParser.parsePlan(sql) # 解析sql语句，生成LogicalPlan
scala> val queryExecution = spark.sessionState.executePlan(logical) # 进行验证和优化，并生成物理计划
scala> print(queryExecution)
== Parsed Logical Plan ==
'Project ['fruit.name, 't1.num]
+- 'Join Inner, ('fruit.id = 't1.fruit_id)
   :- 'UnresolvedRelation `fruit`
   +- 'SubqueryAlias `t1`
      +- 'Aggregate ['fruit_id], ['fruit_id, 'COUNT(1) AS num#6]
         +- 'UnresolvedRelation `orders`

== Analyzed Logical Plan ==
name: string, num: bigint
Project [name#9, num#6L]
+- Join Inner, (id#8 = fruit_id#13)
   :- SubqueryAlias `default`.`fruit`
   :  +- HiveTableRelation `default`.`fruit`, org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe, [id#8, name#9, price#10, amount#11]
   +- SubqueryAlias `t1`
      +- Aggregate [fruit_id#13], [fruit_id#13, count(1) AS num#6L]
         +- SubqueryAlias `default`.`orders`
            +- HiveTableRelation `default`.`orders`, org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe, [id#12, fruit_id#13, create_time#14, consumer_name#15]

== Optimized Logical Plan ==
Project [name#9, num#6L]
+- Join Inner, (id#8 = fruit_id#13)
   :- Project [id#8, name#9]
   :  +- Filter isnotnull(id#8)
   :     +- HiveTableRelation `default`.`fruit`, org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe, [id#8, name#9, price#10, amount#11]
   +- Aggregate [fruit_id#13], [fruit_id#13, count(1) AS num#6L]
      +- Project [fruit_id#13]
         +- Filter isnotnull(fruit_id#13)
            +- HiveTableRelation `default`.`orders`, org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe, [id#12, fruit_id#13, create_time#14, consumer_name#15]

== Physical Plan ==
*(3) Project [name#9, num#6L]
+- *(3) BroadcastHashJoin [id#8], [fruit_id#13], Inner, BuildLeft
   :- BroadcastExchange HashedRelationBroadcastMode(List(cast(input[0, int, false] as bigint)))
   :  +- *(1) Filter isnotnull(id#8)
   :     +- Scan hive default.fruit [id#8, name#9], HiveTableRelation `default`.`fruit`, org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe, [id#8, name#9, price#10, amount#11]
   +- *(3) HashAggregate(keys=[fruit_id#13], functions=[count(1)], output=[fruit_id#13, num#6L])
      +- Exchange hashpartitioning(fruit_id#13, 200)
         +- *(2) HashAggregate(keys=[fruit_id#13], functions=[partial_count(1)], output=[fruit_id#13, count#17L])
            +- *(2) Filter isnotnull(fruit_id#13)
               +- Scan hive default.orders [fruit_id#13], HiveTableRelation `default`.`orders`, org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe, [id#12, fruit_id#13, create_time#14, consumer_name#15]

```

从上面的输出可以看到，这条语句经过了三个阶段，一次生成了 Parsed Logical Plan， Analyzed Logical Plan， Optimized Logical Plan， Physical Plan。

上面每一步的生成结果，无论是 LogicalPlan 还是 PhysicalPlan，都是通过树的形式表示。每一步都是对树进行操作，生成新的树。如果想要深入了解，就必须要了解这些数据结构的原理。spark sql 使用基础类TreeNode 来实现树，在介绍 TreeNode 之前，需要先了解一些基本的语法。



## UML 类

{% plantuml %}

@startuml
TreeNode <|-- QueryPlan
QueryPlan <|-- LogicalPlan
QueryPlan <|-- SparkPlan
TreeNode <|-- Expression
@enduml

{% endplantuml %}

TreeNode： 表示树结构，负责实现树的基本操作

Expression：sql 表达式

QueryPlan：拥有零个或多个 Expression

LogicalPlan ： 逻辑计划，提供了解析表达式操作 

SparkPlan：物理计划，负责生成实际的运行程序

本篇先讲讲 TreeNode 和 QueryPlan 基类的原理，后续的文章会具体讲其他类的原理。



## 自限定泛型

TreeNode是一个自限定的泛型类，首先看看它的类声明

```scala
abstract class TreeNode[BaseType <: TreeNode[BaseType]] extends Product {
    ......
}
```

首先它是一个抽象类，其子类分为两种，逻辑计划 LogicalPlan，物理计划 SparkPlan。

然后看看它的泛型声明，读者对于这种写法有可能比较陌生，我们可以分为两部分理解

1. 它是一个泛型类，泛型类型由 BaseType 表示
2. 它的泛型类型，必须是TreeNode类的子类

自限定泛型类，主要用于封装公共的方法。比如我们有两个类 Apple 和 Banana，它们有个共同的方法，用来创建出新的对象，那么可以把这个方法抽象，作为泛型类的一个方法。

```java
abstract class Fruit<T extends Fruit<T>> {
    // 实例化
    abstract public T make();
}

class Apple extends Fruit<Apple> {
    
    @Override
    public Apple make() {
        return new Apple();
    }
}

class Banana extends Fruit<Banana> {
    
    @Override
    public Banana make() {
        return new Banana();
    }
}
```

可以看到 Fruit 泛型类，定义了 make 抽象方法，子类必须实现这个抽象方法。在子类中 make 方法，它的返回类型都随着子类改变。这就是自限定泛型最常用的场景。



## Case Class 使用

我们继续观察 TreeNode 的声明，发现它还继承了 Product 接口，但是 TreeNode 类并没有实现 Product 的方法，这就需要子类实现。TreeNode 的子类都是 Case Class 类型，这种类是 scala 独有的语法。使用它有下面几个好处：

1. 子类自动实现 apply 和 unapply 方法。实现了 apply 方法意味着对象实例化不需要 new 关键字，实现 unapply 方法意味着支持模式匹配
2. 子类构造方法的参数，都是 public 权限，意味着可以直接访问
3. 子类自动实现 equals 方法，这个方法用来判断两个对象是否相等
4. 子类自动实现 Product 接口，支持遍历构造方法的参数

TreeNode 的子类实现了 Product 接口，所以支持访问构造方法的参数。TreeNode 类提供了 mapProductIterator 方法，接收一个函数用来遍历这些参数

```scala
abstract class TreeNode[BaseType <: TreeNode[BaseType]] extends Product {

  protected def mapProductIterator[B: ClassTag](f: Any => B): Array[B] = {
    // productArity 是属于Product的方法，返回参数的个数
    val arr = Array.ofDim[B](productArity)
    var i = 0
    while (i < arr.length) {
      // productElement 是属于Product的方法，返回返回指定位置的参数
      arr(i) = f(productElement(i))
      i += 1
    }
    arr
  }
}
```



## TreeNode 判断相等

scala 判断相等的使用，涉及到三种 ==，eq，equals。

- eq 的原理只是比较两者的引用是否相等
- equals 是调用了对象的 equals 方法
- == 等价于 equals 方法，只是加上了对 null 的判断

TreeNode 实现了 fastEquals 方法，来判断两个节点是否相等

```scala
abstract class TreeNode[BaseType <: TreeNode[BaseType]] extends Product {
  def fastEquals(other: TreeNode[_]): Boolean = {
    // 首先比较引用，然后调用Case Class实现的equals方法
    this.eq(other) || this == other
  }
}
```



## 偏函数

偏函数是 scala 的一个特殊函数，可以看作是一个残缺的函数，它只能处理一部分的参数。例如：

```scala
def partital:PartialFunction[Int, String] = {
    case 0 => "hello"
}

partital(0) // "hello"
partital(1) // 报错 MatchError
partital.applyOrElse(2, { num:Int => "world" }) // "world"
```

上图定义了一个偏函数，它接收 Int 类型的参数，返回 String 类型的结果。不过它只接收参数值为 0 的情况。

另外偏函数还有个 applyOrElse 方法，它额外接收了一个函数。如果偏函数不处理当前参数，那么就会调用这个函数。



## TreeNode 遍历操作

介绍完基础知识后，现在可以来看看树的遍历操作。当我们生成了逻辑计划 LogicalPlan 后，需要对它进行验证和优化，这些重要的操作都会使用到树的遍历。



### mapChildren 方法

mapChildren 方法接收一个函数参数，负责处理节点，它会返回一个新的节点。

mapChildren 会依次调用函数对子节点操作，根据返回的结果生成一个新的节点。因为要实例一个新节点，需要知道这个节点的构建参数类型，但是每个类的构建参数类型不一样，Spark Sql 利用了 Case Class 的特性，通过 mapProductIterator 方法遍历参数。如果在遍历构造参数时，发现了它的子子节点，就调用函数进行处理，然后保存处理后的新节点，这样就可以按照顺序保存新的构造参数，就很容易的实例化新的节点。

```scala
abstract class TreeNode[BaseType <: TreeNode[BaseType]] extends Product {
    
  // 返回子节点
  def children: Seq[BaseType]
 
  // 返回子节点的set集合
  lazy val containsChild: Set[TreeNode[_]] = children.toSet    
    
  // 遍历子节点 
  def mapChildren(f: BaseType => BaseType): BaseType = {
    // 如果是叶子节点，则返回自身节点
    // 如果是非叶子节点，那么会遍历构造函数的参数。如果参数是子节点，那么递归遍历
    if (children.nonEmpty) {
      var changed = false
      // 调用了mapProductIterator方法，遍历构造函数的参数，返回新的构造参数
      // 这里只会遍历LogicalPlan的实例，并且还必须是子节点
      val newArgs = mapProductIterator {
        // 如果参数是TreeNode子类，并且是该节点的子节点
        case arg: TreeNode[_] if containsChild(arg) =>
          // 递归调用函数遍历
          val newChild = f(arg.asInstanceOf[BaseType])
          // 如果子节点发生变化了，则更改changed标识
          if (!(newChild fastEquals arg)) {
            changed = true
            newChild
          } else {
            arg
          }
          
        case Some(arg: TreeNode[_]) if containsChild(arg) =>
          ...... // 遍历子节点
          
        // 如果参数是Map类型，则从它的values找到
        case m: Map[_, _] => m.mapValues {
          case arg: TreeNode[_] if containsChild(arg) =>
           ...... // 遍历子节点
        }.view.force // `mapValues` is lazy and we need to force it to materialize
        case d: DataType => d // Avoid unpacking Structs
        
        // 如果参数是容器，则遍历容器的元素
        case args: Traversable[_] => args.map {
          case arg: TreeNode[_] if containsChild(arg) =>
            ...... // 遍历子节点
          case tuple@(arg1: TreeNode[_], arg2: TreeNode[_]) =>
            // 检查两个节点是否为子节点，如果是节点，则递归遍历
            ...... // 遍历子节点
          case other => other
        }
          
        case nonChild: AnyRef => nonChild
        case null => null
      }
      // 如果子节点发生变化，则利用新的构造参数，实例化新的节点
      if (changed) makeCopy(newArgs) else this
    } else {
      // 递归结束条件
      this
    }
  }
}
```



### TransformDown 操作

我们定义 transformDown 函数，它接收一个函数参数，这里称为规则，负责对节点进行操作。它返回执行过规则后的新节点。

transformDown 是一个递归函数，递归逻辑如下：

- 递归结束条件：如果是叶子节点，那么使用规则对该节点操作，并且返回操作后的节点。
- 递归继续条件：如果不是叶子节点，那么先使用该规则对该节点操作。对操作后的该节点，继续遍历其子节点，用子节点的返回结果，来构建成新的节点。



```scala
abstract class TreeNode[BaseType <: TreeNode[BaseType]] extends Product {
    
  // 对该节点的所有子节点调用 rule 方法，如果中间节点发生了改变，那么就 copy 节点
  def transform(rule: PartialFunction[BaseType, BaseType]): BaseType = {
    transformDown(rule)
  }
    
  def transformDown(rule: PartialFunction[BaseType, BaseType]): BaseType = {
    val afterRule = CurrentOrigin.withOrigin(origin) {
      // 对当前节点，调用rule函数。
      // 这里rule函数有可能会生成新的节点，新节点的子节点可能不一样
      rule.applyOrElse(this, identity[BaseType])
    }

    if (this fastEquals afterRule) {
      // 如果当前节点没有变化，则继续遍历它的子节点
      mapChildren(_.transformDown(rule))
    } else {
      // 如果当前节点发生改变，需要对改变后的节点进行遍历
      afterRule.mapChildren(_.transformDown(rule))
    }
  }
}
```

上面的 mapChildren 就是递归的遍历子节点，执行函数。如果某个节点的子节点发生改变，那么就返回改变后的新节点。



### TransformUp 操作

transformUp 操作与上面的 transformDown 操作有一点区别。transformDown 是采用前序遍历的，而 transformUp 是后序遍历的。

递归逻辑：

- 递归结束条件：如果是子节点，那么使用该规则执行该节点，并且返回执行规则后的节点
- 递归继续条件：如果有子节点，那么先根据遍历子节点的结果，生成新节点。最后在使用该规则执行新节点



```scala
abstract class TreeNode[BaseType <: TreeNode[BaseType]] extends Product {

  def transformUp(rule: PartialFunction[BaseType, BaseType]): BaseType = {
    // 先遍历子节点
    val afterRuleOnChildren = mapChildren(_.transformUp(rule))
    // 然后遍历当前节点
    if (this fastEquals afterRuleOnChildren) {
      CurrentOrigin.withOrigin(origin) {
        rule.applyOrElse(this, identity[BaseType])
      }
    } else {
      CurrentOrigin.withOrigin(origin) {
        rule.applyOrElse(afterRuleOnChildren, identity[BaseType])
      }
    }
  }
}
```



## QueryPlan 遍历表达式

QueryPlan 在 TreeNode 的基础上，添加了对 Expression 的处理。每个 QueryPlan 包含了零个或多个表达式，这些表达式都是通过树结构组织在一起的。QueryPlan 支持递归遍历这些表达式。

### mapExpressions 方法

mapExpressions 方法接收一个函数参数，这个函数负责处理一个 Expression 参数，然后返回一个新的 Expression 实例。

```scala
abstract class QueryPlan[PlanType <: QueryPlan[PlanType]] extends TreeNode[PlanType] {
    
  def mapExpressions(f: Expression => Expression): this.type = {
    var changed = false

    // 对expression 执行函数
    @inline def transformExpression(e: Expression): Expression = {
      val newE = f(e)
      if (newE.fastEquals(e)) {
        e
      } else {
        changed = true
        newE
      }
    }
   
    // 定义递归函数，用来遍历子表达式
    def recursiveTransform(arg: Any): AnyRef = arg match {
      case e: Expression => transformExpression(e)      // 如果参数是Expression子类，那么继续遍历
      case Some(value) => Some(recursiveTransform(value))
      case m: Map[_, _] => m
      case d: DataType => d // Avoid unpacking Structs
      case seq: Traversable[_] => seq.map(recursiveTransform)  // 如果参数是列表，继续遍历
      case other: AnyRef => other
      case null => null
    }
      
    // 遍历构造参数，执行recursiveTransform函数
    val newArgs = mapProductIterator(recursiveTransform)

    if (changed) makeCopy(newArgs).asInstanceOf[this.type] else this
  }
    
```



### 先序遍历

遍历原理同 transformDown

```scala
abstract class QueryPlan[PlanType <: QueryPlan[PlanType]] extends TreeNode[PlanType] {
    
  def transformExpressions(rule: PartialFunction[Expression, Expression]): this.type = {
    transformExpressionsDown(rule)
  }

  def transformExpressionsDown(rule: PartialFunction[Expression, Expression]): this.type = {
    // 调用mapExpressions 遍历expression子节点
    mapExpressions(_.transformDown(rule))
  }
    
```



### 后续遍历

遍历原理同 transformUp

```scala
abstract class QueryPlan[PlanType <: QueryPlan[PlanType]] extends TreeNode[PlanType] {
  def transformExpressionsUp(rule: PartialFunction[Expression, Expression]): this.type = {
    mapExpressions(_.transformUp(rule))
  }
}
```

