---
title: antrl4 使用原理
date: 2019-04-26 21:39:45
tags: antrl4
categories: antrl4
---

# antrl4 使用原理

Antlr4 是一款开源的框架，用来分析语法。使用者可以自己创建语法规则文件，然后使用 antrl4 生成类文件。这些类实现了将语句按照关键字分词，然后将分词构造成一棵树。使用者在这些类之上封装代码，就可以实现自己的功能。比如下面，我们使用 antrl4 实现一个计算器：

## 四则运算语法

### 定义语法文件

首先我们创建一个Calculator.g4名称的文件，里面定义了计算器的语法格式

```
grammar Calculator;

prog : stat+;

stat:
  expr NEWLINE          # print
  | ID '=' expr NEWLINE   # assign
  | NEWLINE               # blank
  ;

expr:
  expr op=('*'|'/') expr    # MulDiv
  | expr op=('+'|'-') expr        # AddSub
  | INT                           # int
  | ID                            # id
  | '(' expr ')'                  # parenthese
  ;

MUL : '*' ;
DIV : '/' ;
ADD : '+' ;
SUB : '-' ;
ID : [a-zA-Z]+ ;
INT : [0-9]+ ;
NEWLINE :'\r'? '\n' ;
DELIMITER : ';';
WS : [ \t]+ -> skip;
```

prog是整个语法的入口，它表示可以有多行 stat 语句。

stat就是一行语句的格式，它有三种情形，对应于打印语句，赋值语句 和 空行。

expr 表示表达式语句，支持嵌套。



### 语法树

以下面的字符串为输入：

```javascript
a = 12
b = a * 2
a + b
```

它会被解析成一棵树

<img src="calculator-parse-tree.png">

上面的三行语句，对应三个stat节点。

a = 12，匹配assign这个类型规则 。assign这个节点拥有4个子节点， 依次为ID， =, int， NEWLINE。其中的int子节点是匹配了expr的 INT规则

b = a * 2， 也是匹配了assign这个类型规则 。它被分成四个节点： 变量 b， 等号 =，表达式，a * 2， 换行符。

其中 a * 2这个表达式，匹配了 expr 的 MulDiv 规则。而这个表达式，包含了 变量 a 和 数字 2 两个表达式，匹配了 id 规则 和 int 规则。

a + b， 匹配了 print 这个类型规则 expr NEWLINE。



### 实现遍历代码

我们用 antrl4 的命令行，可以生成关于解析语句的类。其中 CalculatorBaseVisitor 类比较重要，我们可以继承它，实现语法树的遍历，来完成计算器的功能

```java
public class CalculatorVisitorImp extends CalculatorBaseVisitor<Integer> {

    // 存储变量的值
    private Map<String, Integer> variable;

    public CalculatorVisitorImp() {
        variable = new HashMap<>();
    }

    // 当遇到print节点，计算出exrp的值，然后打印出来
    @Override
    public Integer visitPrint(CalculatorParser.PrintContext ctx) {
        Integer result  = ctx.expr().accept(this);
        System.out.println(result);
        return null;
    }

    // 分别获取子节点expr的值，然后做加减运算
    @Override
    public Integer visitAddSub(CalculatorParser.AddSubContext ctx) {
        Integer param1 = ctx.expr(0).accept(this);
        Integer param2 = ctx.expr(1).accept(this);
        if (ctx.op.getType() == CalculatorParser.ADD) {
            return param1 + param2;
        }
        else {
            return param1 - param2;
        }
    }

    // 分别获取子节点expr的值，然后做乘除运算
    @Override
    public Integer visitMulDiv(CalculatorParser.MulDivContext ctx) {
        Integer param1 = ctx.expr(0).accept(this);
        Integer param2 = ctx.expr(1).accept(this);
        if (ctx.op.getType() == CalculatorParser.MUL) {
            return param1 * param2;
        }
        else {
            return param1 / param2;
        }
    }

    // 当遇到int节点，直接返回数据
    @Override
    public Integer visitInt(CalculatorParser.IntContext ctx) {
        return Integer.parseInt(ctx.getText());
    }

    // 当遇到Id节点，从变量表获取值
    @Override
    public Integer visitId(CalculatorParser.IdContext ctx) {
        return variable.get(ctx.getText());
    }

    // 当遇到赋值语句，获取右边expr的值，然后将变量的值保存到variable集合
    @Override
    public Integer visitAssign(CalculatorParser.AssignContext ctx) {   
        String name = ctx.ID().getText();
        Integer value = ctx.expr().accept(this);
        variable.put(name, value);
        return null;
    }
}
```

遍历语法树的顺序为

<img src="calculator-visit-tree.png">

### 测试

```java
public class Main {

    public static void main(String[] args) throws IOException {
        String expression = "a = 12\n" +
                "b = a * 2\n" +
                "a + b\n";
        CalculatorLexer lexer = new CalculatorLexer(CharStreams.fromString(expression));
        CommonTokenStream tokens = new CommonTokenStream(lexer);
        CalculatorParser parser = new CalculatorParser(tokens);
        parser.setBuildParseTree(true);
        ParseTree root = parser.prog();
        CalculatorVisitor<Integer> visitor = new CalculatorVisitorImp();
        root.accept(visitor);
    }
}
```

结果输出为：36



## antrl4 基础类

上面介绍了 antrl4 的 基本用法，实现了一个四则运算的功能。但是其中的原理，还没有详细讲解。我们知道 antlr4 会将语句解析成一棵树，但这棵树的数据结构是什么样的，还不清楚。我们首先介绍下树的节点。

树的节点可以主要分为叶子节点和非叶子节点两类。 但是涉及到节点的类比较多，如下图所示

{% plantuml %}
@startuml

interface Tree
interface SyntaxTree
interface ParseTree
interface TerminalNode
class TerminalNodeImpl
class ErrorNodeImpl
interface RuleNode
class RuleContext
class ParserRuleContext
class InterpreterRuleContext
class RuleContextWithAltNum

Tree <|-- SyntaxTree
SyntaxTree <|-- ParseTree
ParseTree <|-- TerminalNode
TerminalNode <|.. TerminalNodeImpl
TerminalNodeImpl <|-- ErrorNodeImpl
ParseTree <|-- RuleNode
RuleNode <|.. RuleContext
RuleContext <|-- ParserRuleContext
ParserRuleContext <|-- InterpreterRuleContext
ParserRuleContext <|-- RuleContextWithAltNum

@enduml
{% endplantuml %}



上述类的解释如下：

- Tree 接口，是所有节点的接口。它定义了获取父节点，子节点，节点数据的接口。
- SyntaxTree 接口，增加了获取当前节点涉及到的分词范围（antrl4 会先将语句分词，然后才将分词解析成树）
- ParseTree 接口，增加了支持Visitor遍历树的接口
- TerminalNode 接口，表示叶子节点，增加了获取当前节点的分词（叶子节点表示字符常量，或者在antrl4文件中的lexer ）
- TerminalNodeImpl 类，实现了TerminalNode 接口，表示正常的叶子节点
- ErrorNodeImpl 类，继承 TerminalNodeImpl 类，表示错误的叶子节点。
- RuleNode 接口，非叶子节点，表示一个句子的语法，对应 antrl4文件中的 parser rule
- RuleContext 类，实现了RuleNode 接口
- ParserRuleContext 类，在 RuleContext 的基础上，实现了查询子节点的方法，并且支持Listener遍历
- InterpreterRuleContext 和 RuleContextWithAltNum 是用于特殊用途的。



我们一般主要使用两个类， TerminalNodeImpl（叶子节点），ParserRuleContext（非叶子节点）。



## Visitor 遍历类

antrl4 提供了visitor 遍历方式，这是典型的访问者设计模式。访问者为每个不同类型的节点，实现不同的访问方法。而每个节点实现统一的访问入口。

ParseTree 接口代表着节点，它的统一访问入口是 accept 方法

```java
public interface ParseTree extends SyntaxTree {
	<T> T accept(ParseTreeVisitor<? extends T> visitor);
}
```



ParseTree的子类会实现 accept 方法，比如叶子节点 TerminalNodeImpl，它是调用了访问者的 visitTerminal 方法。非叶子节点，调用了访问者的 visitChildren 方法。

```java
public class TerminalNodeImpl implements TerminalNode {
	@Override
	public <T> T accept(ParseTreeVisitor<? extends T> visitor) {
		return visitor.visitTerminal(this);
	}
}

public class RuleContext implements RuleNode {
	@Override
	public <T> T accept(ParseTreeVisitor<? extends T> visitor) {
        return visitor.visitChildren(this);
    }
}
```



ParseTreeVisitor 接口，定义了对于不同类型节点的访问接口。

```java
public interface ParseTreeVisitor<T> {
    // 访问数据节点，不区分类型
	T visit(ParseTree tree);
    // 访问非叶子节点
	T visitChildren(RuleNode node);
    // 访问叶子节点
	T visitTerminal(TerminalNode node);
    // 访问出错节点
	T visitErrorNode(ErrorNode node);
}
```



AbstractParseTreeVisitor 类实现了上述接口，它的 visit 方法，只是简单的调用了节点的 accept 方法

```java
public abstract class AbstractParseTreeVisitor<T> implements ParseTreeVisitor<T> {

   public T visit(ParseTree tree) {
      // 节点的accept方法会根据节点的类型，调用visitor的不同方法
      return tree.accept(this);
   }
}
```



对于叶子节点和出错节点，仅仅是返回一个默认值。

```java
public abstract class AbstractParseTreeVisitor<T> implements ParseTreeVisitor<T> {

    public T visitTerminal(TerminalNode node) {
        return defaultResult();
    }
    
	public T visitTerminal(TerminalNode node) {
		return defaultResult();
	}    
    
	protected T defaultResult() {
		return null;
	}    
}
```



对于非叶子节点，会遍历各个子节点，然后将结果聚合整理。访问非叶子节点涉及到递归，它是依照深度优先遍历。

```java
public abstract class AbstractParseTreeVisitor<T> implements ParseTreeVisitor<T> {
    
    public T visitChildren(RuleNode node) {
        // 生成默认值
        T result = defaultResult();
        int n = node.getChildCount();
        for (int i=0; i<n; i++) {
            // 检测是否继续遍历子节点
            if (!shouldVisitNextChild(node, result)) {
                break;
            }
            // 获取子节点
            ParseTree c = node.getChild(i);
            // 遍历子节点，返回子节点的结果
            T childResult = c.accept(this);
            // 合并子节点的结果
            result = aggregateResult(result, childResult);
        }
        return result;
    }

    // 默认值为null
    protected T defaultResult() {
        return null;
    }

    // 合并结果，这里只是返回子节点的结果
    protected T aggregateResult(T aggregate, T nextResult) {
        return nextResult;
    }

    // 默认允许继续访问
    protected boolean shouldVisitNextChild(RuleNode node, T currentResult) {
        return true;
    }
}
```



## 生成的节点类

 回忆一下上面的计算器语法，里面定义了三条语法规则，prog，stat，expr。antrl4 会为每条规则，生成一个 ParserRuleContext  的子类。如果这个语法规则添加了标签，那么为每个标签也生成一个 ParserRuleContext 的子类。这些类之间的关系，如下图所示：

{% plantuml %}
@startuml

class ParserRuleContext

class ProgContext
class StatContext
class ExprContext

class PrintContext
class BlankContext
class AssignContext

class MulDivContext
class AddSubContext
class ParentheseContext
class IdContext
class IntContext

ParserRuleContext <|-- ProgContext
ParserRuleContext <|-- StatContext
ParserRuleContext <|-- ExprContext

StatContext <|-- PrintContext
StatContext <|-- BlankContext
StatContext <|-- AssignContext

ExprContext <|-- MulDivContext
ExprContext <|-- AddSubContext
ExprContext <|-- ParentheseContext
ExprContext <|-- IdContext
ExprContext <|-- IntContext

@enduml
{% endplantuml %}



介绍完每个节点类，我们继续看看这课语法树的结构，也就是这些节点之间怎么联系起来。以 prog 规则为例，它对应  ProgContext 类。因为 prog 规则 可以包含多个 stat 规则，所以它必须提供访问子节点 StatContext 的方法。

```java
public class CalculatorParser extends Parser {
    
	public static class ProgContext extends ParserRuleContext {
         // 返回子节点 stat 列表
		public List<StatContext> stat() {
			return getRuleContexts(StatContext.class);
		}
         // 返回第几个stat规则
		public StatContext stat(int i) {
			return getRuleContext(StatContext.class,i);
		}

	    // 返回该规则的 id 号， RULE_prog是一个常量
        public int getRuleIndex() { return RULE_prog; }
		
	}
}
```

继续看 stat 规则，它对应着 StatContext 类。 因为它为每种情况添加了标签，所以也为每个标签生成了对应的类，这些类都是 StatContext 的子类。

StatContext 类的定义很简单，它只是实现了返回规则 id。

PrintContext 包含了一个 expr 规则 和 NEWLINE 叶子节点，它都提供了对应的访问方法。

```java
public class CalculatorParser extends Parser {
	
    public static class StatContext extends ParserRuleContext {
        // 返回该规则的 id
        public int getRuleIndex() { return RULE_stat; }
	}
    
	public static class PrintContext extends StatContext {
         // 返回 expr 节点
		public ExprContext expr() {
			return getRuleContext(ExprContext.class,0);
		}
         // 返回NEWLINE 叶子节点。
		public TerminalNode NEWLINE() { return getToken(CalculatorParser.NEWLINE, 0); }
	}    
    
    public static class BlankContext extends StatContext {
        .....// 原理类似
    }
    
    public static class AssignContext extends StatContext {
        .....// 原理类似
    }
}
```

上面虽然只分析了 prog 和 stat 规则，但其余规则的原理是一样的。



## 节点的访问方法

上面生成的节点类，都是 ParserRuleContext  的子类，都实现 accept 方法。每个类的实现方法都不一样，比如 ProgContext 类，它的 accept 方法调用了访问者的 visitProg 方法。而 PrintContext 类的 accept 方法对应于访问者的 visitPrint 方法。

```java
public static class ProgContext extends ParserRuleContext {
    @Override
    public <T> T accept(ParseTreeVisitor<? extends T> visitor) {
        if ( visitor instanceof CalculatorVisitor )
            // 调用了visitor的visitProg方法
            return ((CalculatorVisitor<?extends T>)visitor).visitProg(this);
        else
            return visitor.visitChildren(this);
    }
}

public static class PrintContext extends StatContext {
    @Override
    public <T> T accept(ParseTreeVisitor<? extends T> visitor) {
        if ( visitor instanceof CalculatorVisitor ) 
            // 调用 visitPrint 方法
            return ((CalculatorVisitor<? extends T>)visitor).visitPrint(this);
        else 
            return visitor.visitChildren(this);
    }    
}
```



CalculatorBaseVisitor 提供了访问不同节点的方法，默认实现都是调用 visitChildren 方法。它的泛型 T 表示返回的结果类型。使用者一般继承 CalculatorBaseVisitor 类，复写一些方法，来实现自定义的功能（比如上面的四则运算例子）。

```java
public class CalculatorBaseVisitor<T> extends AbstractParseTreeVisitor<T> implements CalculatorVisitor<T> {

	@Override
    public T visitAssign (CalculatorParser.AssignContext ctx) { return visitChildren(ctx); 
    }
	
	@Override public T visitInt(CalculatorParser.IntContext ctx) { return visitChildren(ctx); }
    ......
}
```



## 参考资料

<https://github.com/antlr/antlr4/blob/master/doc/getting-started.md>

<https://github.com/antlr/antlr4/blob/master/doc/parser-rules.md>

<https://www.jianshu.com/p/dc1b68dfe2d7>