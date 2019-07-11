---
title: Spark Sql 表达式介绍
date: 2019-07-11 21:47:46
tags: spark sql, expression
categories: spark sql
---



## 前言

上篇博客介绍了解析 sql 语句生成 LogicalPlan 树的原理，还没有介绍如何解析表达式。LogicalPlan 大都包含了多个表达式，可以认为 LogicalPlan 只是对 sql 语句做了一次粗略的划分，再细一层的划分就是基于表达式的解析。表达式的种类繁多，下面先从简单的单值表达式介绍，然后到复杂的表达式。介绍完表达式的种类，最后再来介绍解析的原理。



## 表达式基类

表达式由 Expression 基类表示，先来看看它的几个重要属性

```scala
abstract class Expression extends TreeNode[Expression] {
  // 表达式的值类型
  def dataType: DataType

  // 引用的列
  def references: AttributeSet = AttributeSet(children.flatMap(_.references.iterator))

  // 检查输入类型，默认成功。子类如果对输入类型有要求，则需要重写
  def checkInputDataTypes(): TypeCheckResult = TypeCheckResult.TypeCheckSuccess

  // 根据输入计算表达式的值    
  def eval(input: InternalRow = null): Any

  // 是否结果确定性，也就是相同的输入必定有同样的值
  // 因为空列表的forall返回true，说明叶子节点默认是结果确定性
  def deterministic: Boolean = children.forall(_.deterministic)
    
}
```

Expression 也是继承 TreeNode，说明表达式也是被组织成数据结构。根据子节点的数量，可以将表达式分为三种：

- 没有子节点，对应 LeafExpression 类
- 只有一个子节点，对应 UnaryExpression 类
- 只有两个子节点，对应 BinaryExpression 类
- 只有三个子节点，对应 TernaryExpression 类（这种情况比较少见）

### 计算表达式的值

UnaryExpression 类为了方便子类实现计算值时，不需要从考虑 null 值特殊情况，复写了 eval 方法

```scala
abstract class UnaryExpression extends Expression {
  // 子类需要实现此方法
  protected def nullSafeEval(input: Any): Any =
    sys.error(s"UnaryExpressions must override either eval or nullSafeEval")
    
  override def eval(input: InternalRow): Any = {
    // 如果子表达式的值为null，那么输出也为null
    // 否则调用nullSafeEval方法计算
    val value = child.eval(input)
    if (value == null) {
      null
    } else {
      nullSafeEval(value)
    }
  }
}
```



BinaryExpression 类同样也复写了 eval 方法，如果有一个子节点的值为null，那么它的输出值也为null。子类同样需要实现 nullSafeEval 方法。



### 检查输入值

为了更方便的检查输入类型，spark sql 提供了 ExpectsInputTypes 接口。子类只需要复写 inputTypes 方法，定义各个输入值的类型即可。

```scala
trait ExpectsInputTypes extends Expression {
  // 返回类型列表，第 i 个类型对应着第 i 个输入值
  def inputTypes: Seq[AbstractDataType]

  override def checkInputDataTypes(): TypeCheckResult = {
    // 检查类型是否不匹配
    val mismatches = children.zip(inputTypes).zipWithIndex.collect {
      case ((child, expected), idx) if !expected.acceptsType(child.dataType) =>
        s"......"
    }

    if (mismatches.isEmpty) {
      TypeCheckResult.TypeCheckSuccess
    } else {
      TypeCheckResult.TypeCheckFailure(mismatches.mkString(" "))
    }
  }
}
```



## 单值表达式

### 星号

当我们编写 sql 语句时，经常会用到 SELECT * 语句，这个星号就会被解析成 UnresolvedStar 实例，表示选择所有列。星号会被后面的 Analyser 解析成字段列表。

### 字段

比如我们编写的 sql 语句如下：

```sql
SELECT name, price FROM fruit WHERE price > 10 ORDER BY PRICE; 
```

这条语句涉及到了两个字段 name 和 price，这两个字段会被解析成 UnresolvedAttribute 实例。我们知道WHERE 语句被会被解析成 Filter 实例，Filter 会包含 price 字段的实例。SELECT 语句会被解析成 Project 实例，Project 会包含 name 和 price 两个字段的实例。

关于字段的表达式，还支持前缀表名，比如我们有一个子查询，这里的 t1.NAME 仍然会被解析成 UnresolvedAttribute。

```sql
SELECT t1.name FROM (SELECT name, price FROM fruit WHERE price > 10) t1
```



对于字段表达式，它的基类为Attribute，有两个重要属性，  所属表名和列名。

UnresolvedAttribute 表示还没被 analyse，所以它的表名和列名都没有。

当 UnresolvedAttribute 被analyse 之后，它的表名和列名都会生成。比如上面的第一条 sql 数据，以 price 字段为例，它的列名为 price，表名为 fruit。

UnresolvedAttribute 类的定义如下，有一个字符串列表 nameParts。读者可能好奇为什么会是列表，如果是有前缀表名，那么 nameParts 的值就为 ["t1", "name"]

```scala
case class UnresolvedAttribute(nameParts: Seq[String]) extends Attribute with Unevaluable {
    .....
}
```



### 集合取值运算

Spark Sql 的字段类型支持集合类型，比如 Map，Array。它们的取值符号都为点号，

```sql
spark-sql> select * from fruit;
id  name    price
2	banana	2.5
1	apple	1.5

# 我们使用map函数，新建了一个Map类型的字段，key为name的值，value为id的值 
spark-sql> select map(name, id).apple from fruit;
NULL
1
```

这个表达式会被解析成 UnresolvedExtractValue 实例，它的字表达式是map函数。



这里说下特殊情况，假设我们有一个 Map 类型的字段 items，执行的 sql 语句如下：

```sql
SELECT items.apple FROM fruit
```

items.apple 会被解析成 UnresolvedAttribute 实例，虽然它确实是集合取值运算。不过在后面的 Analyser 会修正这一点，它首先会匹配 items 不是表名，然后匹到它是字段名，所以最终会认为它是集合取值运算。



### 常数

当 sql 中遇到 整数型，字符串型 或其他数值型的表达式，就会被解析成 Literal 实例。

```scala
case class Literal (value: Any, dataType: DataType) 
```

它使用Any类型保存值，并用 dataType 描述类型。虽然有不同的数据类型，但是有些数据类型可以隐式转换的。



### 函数

我们以 Md5 函数为例，

```sql
SELECT MD5(name) FROM fruit
```

上述md5函数的语句，会被解析成 UnresolvedFunction 实例。

```scala
case class UnresolvedFunction(
    name: FunctionIdentifier,  // 函数名称
    children: Seq[Expression],  // 函数参数
    isDistinct: Boolean)   // 是否函数参数有 distinct 关键字
```



Spark Sql 会根据函数名称，在注册中心找到对应的函数。Md5 函数的定义如下：

```scala
case class Md5(child: Expression) extends UnaryExpression with ImplicitCastInputTypes {

  override def dataType: DataType = StringType  // 输出类型为String

  override def inputTypes: Seq[DataType] = Seq(BinaryType) // 输入类型为字节数组

  // 复写 nullSafeEval 方法
  protected override def nullSafeEval(input: Any): Any =
    UTF8String.fromString(DigestUtils.md5Hex(input.asInstanceOf[Array[Byte]]))
}
```



### 别名

sql 语句支持别名，这些别名会被解析成 Alias。



### 多个别名

sql 语句支持多个别名，这些别名会被解析成 MultiAlias。



## 命名表达式

这类表达式比较特殊，它只对应 SELECT 语句后面的选择表达式。 命名表达式的基类是 NamedExpression，它都至少有一个名称，和一个可选的表名。

```scala
trait NamedExpression extends Expression {
  // 名称
  def name: String
  // 所属表名
  def qualifier: Option[String]
}
```



它的子类如下所示：

{% plantuml %}

@startuml
interface NamedExpression
abstract class Attribute
abstract class Star

NamedExpression <|-- Attribute
Attribute <|-- UnresolvedAttribute
Attribute <|-- AttributeReference
NamedExpression <|-- Alias
NamedExpression <|--  UnresolvedAlias
NamedExpression <|-- Star
Star <|-- UnresolvedStar
@enduml

{% endplantuml %}

SELECT 语句中的选择表达式，在被解析时，都会生成 NamedExpression 子类。注意到 UnresolvedAlias这个特殊的类，它只是起到封装的作用，将其他类型的表达式封装成 NamedExpression 的子类，在解析LogicalPlan里提到过。



## 数值运算

sql 支持常见的数值运算，每种运算对应不同的实例。下面列出了比较常见的类型

| 运算类型 | 表达式类  |
| -------- | --------- |
| 加法     | Add       |
| 减法     | Subtract  |
| 乘法     | Multiply  |
| 除法     | Divide    |
| 取余     | Remainder |



## 逻辑表达式

当表达式返回的值为 Boolean 类型，那么就可以称为逻辑表达式，由 Predicate 接口表示。

```scala
trait Predicate extends Expression {
  override def dataType: DataType = BooleanType
}
```

逻辑表达式的种类有多种，首先介绍下简单的情况，然后再介绍复杂的。



### 数值比较

sql 语句也支持数值之间的比较运算，下面列出了比较常见的情况

| 运算类型 | 表达式类 |
| -------- | -------- |
| 等于     | EqualTo  |
| 小于     | LessThan |
| .......  | .......  |



### 判断表达式

sql 语句支持 BETWEEN  AND 语句，IN 语句来判断范围，还有常见的 IS 或 IS NOT 语句。

其中 BETWEEN  AND 语句会被解析成一个大于等于的实例，和一个小于等于的实例，然后将两个实例通过 And 逻辑运算连接到一起。



## 逻辑运算

逻辑运算有以下三种

| 运算类型 | 表达式类 |
| -------- | -------- |
| And      | And      |
| Or       | Or       |
| Not      | Not      |

我们以 And 为例，来看看它是如何计算表达式的值

```scala
case class And(left: Expression, right: Expression) extends BinaryOperator with Predicate {
  // 输入类型必须为 Boolean 类型
  override def inputType: AbstractDataType = BooleanType
 
  override def eval(input: InternalRow): Any = {
    // 计算第一个子表达式的值
    val input1 = left.eval(input)
    if (input1 == false) {
       false
    } else {
      // 计算第一个子表达式的值
      val input2 = right.eval(input)
      if (input2 == false) {
        false
      } else {
        if (input1 != null && input2 != null) {
          true
        } else {
          null
        }
      }
    }
  }
} 
```



## 解析原理

表达式的解析也是基于语法树生成的，所以解析原理还是需要理解 antlr 文件。 

我们以下面的 sql 语句为例，

```sql
SELECT NAME, PRICE-1 AS DISCOUNT, 'favorite' FROM fruit WHERE PRICE > 2 AND NAME = 'apple'
```

它的语法树如下



### namedExpression 规则

这条语句的 NAME, PRICE-1 AS DISCOUNT, 'favorite' 都会匹配 namedExpression 语法规则，它包含了可选的别名，还有一个 子规则 expression。

visitNamedExpression 方法定义了访问原理，会返回 Expression 的子类。

```scala
override def visitNamedExpression(ctx: NamedExpressionContext): Expression = withOrigin(ctx) {
  // 解析子表达式
  val e = expression(ctx.expression)
  if (ctx.identifier != null) {
    // 如果指定了一个别名，那么返回Alias实例
    Alias(e, ctx.identifier.getText)()
  } else if (ctx.identifierList != null) {
    // 如果有多个别名（需要以括号将这些别名包起来），那么返回MultiAlias实例
    MultiAlias(e, visitIdentifierList(ctx.identifierList))
  } else {
    // 返回子表达式
    e
  }
}
```

而 expression 规则最后会按照 booleanExpression 规则解析，并且 WHERE 后面的过滤表达式，也会匹配为 booleanExpression 规则。booleanExpression 规则主要有两类格式，一种是包含逻辑运算符的，另一种是基础的表达式。

### booleanExpression 规则

如果是第一种格式，比如包含 AND 或 OR 关键字。这类语句的解析稍微复杂，因为spark sql 会做一部分的优化。我们知道antrl4 是匹配语法规则时，它是用左递归的方式匹配。下面以 booleanExpression 规则为例，

```shell
booleanExpression
    : NOT booleanExpression                                        #logicalNot
    | EXISTS '(' query ')'                                         #exists
    | valueExpression predicate?                                   #predicated
    | left=booleanExpression operator=AND right=booleanExpression  #logicalBinary
    | left=booleanExpression operator=OR right=booleanExpression   #logicalBinary
    ;
```

假设有一个表达式语句

```sql
NAME = 'pear' AND NAME = 'apple' AND NAME = 'orange' AND NAME = 'banana' AND NAME = 'strawberry'
```

那么它会被解析



很明显这颗树左右不对称，而且左子树的深度很大。这样递归遍历树的时候，容易造成栈溢出。spark sql 针对这种情况，会尽可能的平衡这棵树，比如上面连续的 AND 表达式，会被优化成如下图所示。不过 spark sql 只能优化，从跟节点开始，连续为AND 或者连续为OR的这一段路径。具体程序就不介绍了，定义在 visitLogicalBinary 方法中。



如果是基础表达式，则对应于 predicated 格式。predicated格式的 predicate 规则，用来匹配 IN，BETWEEN AND 等语句。

```scala
override def visitPredicated(ctx: PredicatedContext): Expression = withOrigin(ctx) {
  // 遍历子规则 valueExpression
  val e = expression(ctx.valueExpression)
  if (ctx.predicate != null) {
    // 如果满足 predicate 格式的语句，则调用 withPredicate 方法生成Expression实例
    withPredicate(e, ctx.predicate)
  } else {
    e
  }
}
```



### valueExpression 规则

继续看子规则 valueExpression 的原理，valueExpression 有多种规则，能够匹配四则运算，大小等于的比较操作，还有异或预算。对于这些运算的规则，访问的原理很简单，只是生成了对应数值运算表达式。



### primaryExpression 规则

继续遍历子节点 primaryExpression，它的规则比较多，这里仅仅介绍常见的几种。

1. columnReference 规则负责匹配列名，会返回字段表达式
2. functionCall 规则负责匹配函数，会返回函数表达式
3. star 规则负责匹配星号，用来表示选择所有列，会返回星号表达式
4. constantDefault 规则负责匹配常量，返回 Literal 类
5. dereference 规则匹配点号，返回带前缀表名的字段或集合取值运算

