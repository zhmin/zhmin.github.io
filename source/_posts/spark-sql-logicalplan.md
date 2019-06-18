---
title: Spark Sql  LogicalPlan 原理
date: 2019-06-18 21:58:42
tags: spark sql,logical plan
categories: spark sql
---

## 前言

Spark Sql 会使用 antrl 解析 sql 语句，生成一颗树，树的节点由 LogicalPlan 类表示。后面的验证和优化操作，都是基于这棵树进行的。下面来看看 LogicalPlan 的组成和种类

## UML 类

{% plantuml %}

@startuml
TreeNode <|-- QueryPlan
QueryPlan <|-- LogicalPlan
TreeNode <|-- Expression
@enduml

{% endplantuml %}

下面依次介绍这些类：

TreeNode： 表示树结构的节点，是一个泛型类

Expression：sql中的简单表达式

QueryPlan：拥有零个或多个 Expression

LogicalPlan ： 提供了解析操作   



## 解析 Sql 实例

这里通过解析常见的 sql 语句，来看看是如何生成 LogicalPlan 树。

```sql
 CREATE TABLE fruit (
     id INT, 
     name STRING, 
     price FLOAT,
     amount INT
 );

 CREATE TABLE orders (
 	id INT,
    fruit_id INT,
    create_time TIMESTAMP,
    consumer_name STRING
 );
```



### 查询语句一

执行下列语句

```sql
SELECT * FROM fruit ORDER BY ID LIMIT 10; -- 按序列出前面10个水果
```

解析过程如下：

```shell
== Parsed Logical Plan ==
'GlobalLimit 10
+- 'LocalLimit 10
   +- 'Sort ['ID ASC NULLS FIRST], true
      +- 'Project [*]
         +- 'UnresolvedRelation `fruit`

== Analyzed Logical Plan ==
id: int, name: string, price: float, amount: int
GlobalLimit 10
+- LocalLimit 10
   +- Sort [ID#0 ASC NULLS FIRST], true
      +- Project [id#0, name#1, price#2, amount#3]
         +- SubqueryAlias `default`.`fruit`
            +- HiveTableRelation `default`.`fruit`, org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe, [id#0, name#1, price#2, amount#3]
```



首先看初步生成的结果：

Limit 语句被解析成了 GlobalLimit 和 LocalLimit 两个节点，LocalLimit 节点表示每个分区的数目限制，GlobalLimit 节点表示总的数目限制。

ORDER BY 语句被解析成了 Sort 节点，里面包含了排序的字段和排序的方向。

SELECT 语句中选择的列名，被解析成了 Project 实例。

表名被解析成了 UnresolvedRelation 节点。



再来看看解析过后的结果：

上一步的 UnresolvedRelation 节点，被解析成了 SubqueryAlias 节点，它表示一个子查询。这个子查询就是一张 Hive 表，由 HiveTableRelation 节点表示。

上一步的 Project 节点，它表示的列由 * 号表示，但是经过解析，它被变成了表里的各个列。



### 查询语句二

执行下列语句

```sql
SELECT fruit.name, orders.create_time, orderes.consumer_name  FROM orders INNER JOIN  fruit ON orders.fruit_id = fruit.id WHERE create_time > '2019-06-17';
```

解析过程如下：

```shell
== Parsed Logical Plan ==
'Project ['fruit.name, 'orders.create_time, 'orders.consumer_name]
+- 'Filter ('create_time > 2019-06-17)
   +- 'Join Inner, ('orders.fruit_id = 'fruit.id)
      :- 'UnresolvedRelation `orders`
      +- 'UnresolvedRelation `fruit`

== Analyzed Logical Plan ==
name: string, create_time: timestamp, consumer_name: string
Project [name#41, create_time#38, consumer_name#39]
+- Filter (cast(create_time#38 as string) > 2019-06-17)
   +- Join Inner, (fruit_id#37 = id#40)
      :- SubqueryAlias `default`.`orders`
      :  +- HiveTableRelation `default`.`orders`, org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe, [id#36, fruit_id#37, create_time#38, consumer_name#39]
      +- SubqueryAlias `default`.`fruit`...
```



这里主要看看初步结果：

WHERE 语句被解析成了 Filter 节点，它包含了过滤的表达式

INNER JOIN 语句 被解析成了 Join 节点，它包含了 join 的表达式，并且它还有两个 UnresolvedRelation 子节点，代表着 需要 join 的两张表。



## 常见的 LogicalPlan 节点



### Project 节点

```scala
case class Project(projectList: Seq[NamedExpression], child: LogicalPlan)
```

Project 包含了多个命名表达式，由 NamedExpression 表示。NamedExpression 有多个子类，

{% plantuml %}

@startuml
interface NamedExpression
abstract class Attribute
abstract class Star

NamedExpression <|-- Attribute
Attribute <|-- UnresolvedAttribute
Attribute <|-- AttributeReference
NamedExpression <|-- Alias
NamedExpression <|-- Star
Star <|-- UnresolvedStar
Star <|-- ResolvedStar
@enduml

{% endplantuml %}



Attribute 表示列名，它有两个子类 UnresolvedAttribute 和 AttributeReference，分别表示未解析和解析过后的。

Alias 表示别名。

Star 表示 星号，它有两个子类 UnresolvedStar 和 ResolvedStar，分别表示未解析和解析过后的。



### 表名节点

在 analyse 之前，表名由 UnresolvedRelation 表示。

在 analyse 之后，表名由 SubqueryAlias 表示。



### Join 节点

```scala
case class Join(
    left: LogicalPlan,
    right: LogicalPlan,
    joinType: JoinType,
    condition: Option[Expression])
```

Join 包含两个子节点，join 类型和 join 条件。join type 有下列几种：

- inner join
- left outer join
- right outer join
- full outer join
- left semi join
- left anti join

具体含义可以参见此篇文章 <http://sharkdtu.com/posts/spark-sql-join.html>



### Filter 节点

```scala
case class Filter(condition: Expression, child: LogicalPlan)
```

Filter 节点包含了表达式，就是对应 WHERE 语句。

 

### Sort 节点

```scala
case class Sort(
    order: Seq[SortOrder],
    global: Boolean,
    child: LogicalPlan)
```

Sort 节点对应于 ORDER BY 语句。order 属性是指排序的字段，也可以是表达式。global 属性表示是否为全局的排序，还是只是分区间的排序。



### Distinct 节点

```scala
case class Distinct(child: LogicalPlan)
```

Distinct 节点对应于 DISTINCT 语句。



## Expression 表达式

LogicalPlan 有些节点会包含了表达式，比如 Filter 节点包含了过滤表达式，Project 节点包含了多个表达式。Expression 作为表达式，主要的目的是提供计算的功能。它接收数据，然后经过计算返回表达式的值。

我们首先来看看 expression 在 antrl 文件中的定义

```antrl
expression
    : booleanExpression
    ;

booleanExpression
    : NOT booleanExpression                                        #logicalNot
    | EXISTS '(' query ')'                                         #exists
    | valueExpression predicate?                                   #predicated
    | left=booleanExpression operator=AND right=booleanExpression  #logicalBinary
    | left=booleanExpression operator=OR right=booleanExpression   #logicalBinary
    ;

valueExpression
    : primaryExpression                                                                      #valueExpressionDefault
    | operator=(MINUS | PLUS | TILDE) valueExpression                                        #arithmeticUnary
    | left=valueExpression operator=(ASTERISK | SLASH | PERCENT | DIV) right=valueExpression #arithmeticBinary
    | left=valueExpression operator=(PLUS | MINUS | CONCAT_PIPE) right=valueExpression       #arithmeticBinary
    | left=valueExpression operator=AMPERSAND right=valueExpression                          #arithmeticBinary
    | left=valueExpression operator=HAT right=valueExpression                                #arithmeticBinary
    | left=valueExpression operator=PIPE right=valueExpression                               #arithmeticBinary
    | left=valueExpression comparisonOperator right=valueExpression                          #comparison
    ;
```

上述的 antrl 文件定义了表达式的格式，spark sql 在遍历 antrl 生成的语法树时，会将对应格式的表达式转换为Expression 子类。

比如上面的 booleanExpression 中的 logicalNot 格式，它会返回一个 Not 节点。

```scala
override def visitLogicalNot(ctx: LogicalNotContext): Expression = withOrigin(ctx) {
  Not(expression(ctx.booleanExpression()))
}
```

而 logicalBinary 格式，则会返回 And 节点 或 Or 节点。

上面的 valueExpression 中的 arithmeticUnary 格式，匹配常见的运算表达式

```scala
override def visitArithmeticBinary(ctx: ArithmeticBinaryContext): Expression = withOrigin(ctx) {
  val left = expression(ctx.left)
  val right = expression(ctx.right)
  // 根据元算符号，返回对应的Expression子类
  ctx.operator.getType match {
    case SqlBaseParser.ASTERISK =>
      Multiply(left, right)
    case SqlBaseParser.SLASH =>
      Divide(left, right)
    case SqlBaseParser.PERCENT =>
      Remainder(left, right)
    case SqlBaseParser.DIV =>
      Cast(Divide(left, right), LongType)
    case SqlBaseParser.PLUS =>
      Add(left, right)
    case SqlBaseParser.MINUS =>
      Subtract(left, right)
    case SqlBaseParser.AMPERSAND =>
      BitwiseAnd(left, right)
    case SqlBaseParser.HAT =>
      BitwiseXor(left, right)
    case SqlBaseParser.PIPE =>
      BitwiseOr(left, right)
  }
}
```

上面还有 primaryExpression 的定义没有写出来，它表示列名，常量，或者调用函数等。



## QueryPlan 原理

QueryPlan 在 TreeNode 的基础上，添加了对 Expression 的处理。它支持递归的遍历所有的子表达式

```scala
abstract class QueryPlan[PlanType <: QueryPlan[PlanType]] extends TreeNode[PlanType] {
    
  def transformExpressions(rule: PartialFunction[Expression, Expression]): this.type = {
    transformExpressionsDown(rule)
  }

  def transformExpressionsDown(rule: PartialFunction[Expression, Expression]): this.type = {
    // 调用mapExpressions 遍历expression子节点
    mapExpressions(_.transformDown(rule))
  }
    
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