---
title: Apache Shiro 框架介绍
date: 2020-04-08 22:28:57
tags: shiro
categories: shiro
---



## 前言

Apache Shiro 是 apache 开发的新的用户认证和权限校检框架，相比于 Spring Security 框架更加的简洁易用。这里会先介绍下 shiro 的总体架构和简单示例，读者可以了解到 shiro 的基本概念和框架。为了避免涉及到的知识点过多，这里不涉及 spring web 的集成，后续的文章会详细讲到。



## 架构总览

<img src="shrio-flow.svg">



上图每个矩形都代表着一个模块，这里从上到下依次讲解。

`Subject`在 shiro 框架里就是代表着用户，只不过它为了避免与其它包产生命名冲突，才采用了`Subject`这个概念。它提供了身份认证，权限检查，登录登出，session等接口，我们在使用 shiro 框架时，只需要和`Subject`交互就可以了。

`DefaultSecurityManager`作为 shiro 最核心的类，是整个系统的门面类。上面`Subject`的所有接口调用，都是转发给它处理的。

当需要确认用户身份时，比如验证用户名和密码。`DefaultSecurityManager`会去调用`ModularRealmAuthenticator`类的方法，然后`ModularRealmAuthenticator`会从`Realm`获取用户的身份信息，最后执行身份验证，比如验证密码是否正确。

当需要检查用户是否有权限时，比如判断该用户是否有写权限。`DefaultSecurityManager`会去调用`ModularRealmAuthorizer`类的方法，然后`ModularRealmAuthorizer`会从`Realm`获取该用户的权限信息，最后检查用户是否有该权限。

`Realm`是 shiro 提出的概念，它表示保存用户信息的地方。当要执行身份验证或权限检查时，都会先去它这里获取信息后，才能对比和检查。我们可以通过自定义`Realm`，实现自己的用户管理。



## 示例

下面展示了一个简单的使用例子，摘抄自官网，https://shiro.apache.org/10-minute-tutorial.html。

```java
// 获取Subject
Subject currentUser = SecurityUtils.getSubject();

// 判断是否已经登录，如果没有则需要进行身份验证
if ( !currentUser.isAuthenticated() ) {
    // 这里使用UsernamePasswordToken，表示以用户名和密码的方式进行验证
    UsernamePasswordToken token = new UsernamePasswordToken("lonestarr", "vespa");
    // 执行登录操作，如果认证失败则会抛出AuthenticationException异常
    currentUser.login(token);
}

// 登录成功后，保存用户的id到session里
Session session = currentUser.getSession();
session.setAttribute( "user_id", 122223);

// 获取sessionId，后面可以通过直接通过它来获取用户信息
Serializable sessionId = session.getId();

// 这里通过sessionId，即可获取用户id
SecurityManager securityManager = SecurityUtils.getSecurityManager();
SessionKey sessionKey = new DefaultSessionKey(sessionId);
Session session = securityManager.getSession(sessionKey);
String userid = (Integer) session.getAttribute("user_id");

// 最后登出操作，这里会将其关联的session删除掉
currentUser.logout(); 
```





## 线程安全

从上面的代码可以看到，我们获取`Subject`或`SecurityManager`都是调用`SecurityUtils`的静态方法获取的。之所以 shiro 这样设计，是为了提供线程粒度的隔离。 shrio 允许不同的线程拥有者不同的`Subject`和`SecurityManager`，这样就能支持多线程。在和 spring web 集成时，每一次请求都会新建一个线程处理。

```java
public abstract class SecurityUtils {
    
    private static SecurityManager securityManager;
    
    public static SecurityManager getSecurityManager() throws UnavailableSecurityManagerException {
        // 先从ThreadContext中获取SecurityManager
        SecurityManager securityManager = ThreadContext.getSecurityManager();
        // 如果当前线程没有独立设置SecurityManager，则使用单例模式的securityManager
        if (securityManager == null) {
            securityManager = SecurityUtils.securityManager;
        }
        if (securityManager == null) {
            throw new UnavailableSecurityManagerException(msg);
        }
        return securityManager;
    }
    
    public static Subject getSubject() {
        // 先从ThreadContext中获取用户
        Subject subject = ThreadContext.getSubject();
        if (subject == null) {
            // 如果没有，则创建一个新的Subject
            // 并且将其保存到ThreadContext中
            subject = (new Subject.Builder()).buildSubject();
            ThreadContext.bind(subject);
        }
        return subject;
    }
}
```



上面涉及到了`ThreadContext`类，顾名思义它保存着只属于当前线程的数据，所以会用到`ThreadLocal`类型。下面的代码可以看到它使用了`ThreadLocal<Map>`类型保存线程属性

```java
public abstract class ThreadContext {
    
    private static final ThreadLocal<Map<Object, Object>> resources = new InheritableThreadLocalMap<>();
    
    private static Object getValue(Object key) {
        Map<Object, Object> perThreadResources = resources.get();
        return perThreadResources != null ? perThreadResources.get(key) : null;
    }
    
    public static void put(Object key, Object value) {
        // 保证初始化
        ensureResourcesInitialized();
        // 添加值
        resources.get().put(key, value);
    }
}
```



`ThreadContext`存储着两个很重要的属性，`Subject` 和 `SecurityManager`。它单独为这两个属性提供了操作方法

```java
public abstract class ThreadContext {
    
    // 保存 SecurityManager
    public static void bind(SecurityManager securityManager) {
        if (securityManager != null) {
            put(SECURITY_MANAGER_KEY, securityManager);
        }
    }
    
    // 删除 SecurityManager
    public static SecurityManager unbindSecurityManager() {
        return (SecurityManager) remove(SECURITY_MANAGER_KEY);
    }
    
    // 保存 Subject
    public static void bind(Subject subject) {
        if (subject != null) {
            put(SUBJECT_KEY, subject);
        }
    }
    
    // 删除 Subject
    public static Subject unbindSubject() {
        return (Subject) remove(SUBJECT_KEY);
    }
}
```


