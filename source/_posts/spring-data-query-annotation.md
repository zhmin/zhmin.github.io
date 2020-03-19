---
title: Spring Data Jpa 使用注解实现自定义查询
date: 2020-03-19 18:18:39
tags: spring
categories: spring
---



## 前言

最近看了一个 spring boot 的开源项目，项目地址为`https://github.com/elunez/eladmin-web`，里面有些思想很值得学习。比如它使用了注解，来快速的生成查询条件，使用起来非常方便，可以在项目开发中引用，这篇文章就介绍了注解的使用方法。



## 使用示例

下面列举一个简单的例子，不涉及到联表查询。

首先定义一个包含查询字段的类，然后使用注解来指定查询方式。

```java
@Data
public class BookQueryCriteria {

    // 等于
    @Query
    private String author;

    // 小于等于
    @Query(type = Query.Type.GREATER_THAN, propName = "publishDate")
    private Date startDate;

   // IN 查询
    @Query(type = Query.Type.IN)
    private List<String> titles;
}
```



创建 repository，需要继承 `JpaSpecificationExecutor`才能支持动态查询

```java
public interface BookRespository extends JpaRepository<Book, Integer>, JpaSpecificationExecutor<Book> {
    
}
```



使用 repository 查询

```java
QueryCriteria criteria = new QueryCriteria()
criteria.setAuthor("作家A");
criteria.setStartTime("2020-01-01");
List<String> titles = new ArrayList<>();
titles.add("titleA");
titles.add("titleB")
criteria.setTitles(titles);

repositry.findAll(new Specification() { 
    @Override
    Predicate toPredicate(Root<T> root, CriteriaQuery<?> query, CriteriaBuilder criteriaBuilder) {
        return QueryHelp.getPredicate(root,criteria,criteriaBuilder);   
    }
});
```

如果支持 lambda 函数，代码可以进一步简化。因为`Specification`是一个接口，只需要实现`toPredicate`方法。

```java
repositry.findAll((root, criteriaQuery, criteriaBuilder) -> 
                  QueryHelp.getPredicate(root,criteria,criteriaBuilder));
```



## Query 注解

在了解初步使用后，再来看看它是如何实现的。首先从 Query 注解来开始，它定义了查询方式和查询字段。

```java
@Target(ElementType.FIELD)
@Retention(RetentionPolicy.RUNTIME)
public @interface Query {
    
    // 查询字段，默认为同名字段
    String propName() default "";
    
    // 查询方式，有 = , <=, >=, like, in 等查询方式
    Type type() default Type.EQUAL;
    
}
```

下面还定义了查询方式

```java
    enum Type {
        NOT_NULL            // 不为空  
        EQUAL,              // 等于
        NOT_EQUAL,          // 不等于
        GREATER_THAN,       // 大于等于
        LESS_THAN,          // 小于等于
        LESS_THAN_NQ        // 小于
        IN,                 // 包含
        BETWEEN，           // between        
        INNER_LIKE,         // 中模糊查询
        LEFT_LIKE,          // 左模糊查询，后缀匹配
        RIGHT_LIKE,         // 右模糊查询，前缀匹配
    }
```



## 注解生效

在定义好 Query 注解后， `eladmin`通过`QueryHelp`，遍历字段的注解来生成 `Predicate`实例。

```java
public class QueryHelp {
    public static <R, Q> Predicate getPredicate(Root<R> root, Q query, CriteriaBuilder cb) {
        // 查询条件列表
        List<Predicate> list = new ArrayList<>();
        // 获取类的字段列表
        List<Field> fields = getAllFields(query.getClass(), new ArrayList<>());
        for (Field field : fields) {
            boolean accessible = field.isAccessible();
            field.setAccessible(true);
            // 获取Query注解
            Query q = field.getAnnotation(Query.class);
            if (q != null) {
                // 获取查询字段，如果没有指定propName，则使用同名
                String propName = q.propName();
                String attributeName = isBlank(propName) ? field.getName() : propName;
                // 获取查询值
                Object val = field.get(query);
                // 获取字段类型
                Class<?> fieldType = field.getType();
                switch (q.type()) {
                    case EQUAL:
                        Expression<fieldType> expression = root.get(attributeName).as((Class<? extends Comparable>) fieldType);
                        Predicate predicate = cb.equal(expression, val);
                        list.add(predicate);
                        break;
                    case GREATER_THAN:
                        //.....
                }
            }
            field.setAccessible(accessible);
        }
        int size = list.size();
        // 使用and组合查询条件，表示查询条件列表都要满足
        return cb.and(list.toArray(new Predicate[size]));
    }
}        
```



上面的代码只是展示了 `EQUAL`查询条件的处理，别的条件处理原理都相同。`eladmin`使用反射来遍历字段，获取它们的注解。根据注解不同，生成对应的`Predicate`实例，最后使用 `and`将这些条件组合在一起。

