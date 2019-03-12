---
title: Kafka ConsumerNetworkClient 原理
date: 2019-03-12 21:29:12
tags: kafka, consumer, network, client
categories: kafka
---

# Kafka ConsumerNetworkClient 原理

Kafka消费者会使用ConsumerNetworkClient发送和处理请求。ConsumerNetworkClient是在NetworkClient之外封装了一层，提供了异步的请求方法。它每次发送请求，会返回RequestFuture。RequestFuture实现了类似Future的功能，而且 支持添加事件回调函数。



## RequestFuture 原理

RequestFuture实现了类似Future的功能，它使用了CountDownLatch来实现等待和通知的功能。

```java
public class RequestFuture<T> implements ConsumerNetworkClient.PollCondition {
    private static final Object INCOMPLETE_SENTINEL = new Object();
    // result保存了结果
    private final AtomicReference<Object> result = new AtomicReference<>(INCOMPLETE_SENTINEL);
    // CountDownLatch变量，用来执行等待和通知
    private final CountDownLatch completedLatch = new CountDownLatch(1);
    
    // 等待完成
    public boolean awaitDone(long timeout, TimeUnit unit) throws InterruptedException {
        // 使用了CountDownLatch的await方法实现等待
        return completedLatch.await(timeout, unit);
    }
    
    // 判断是否完成，INCOMPLETE_SENTINEL表示未完成
    public boolean isDone() {
        return result.get() != INCOMPLETE_SENTINEL;
    }
    
    // 获取值
    public T value() {
        if (!succeeded())
            throw new IllegalStateException("Attempt to retrieve value from future which hasn't successfully completed");
        return (T) result.get();
    }    
    
}
```



### 管理监听器

RequestFuture在Future基础之上，还添加了监听器的功能，这使得编程回调更加方便。

```java
public class RequestFuture<T> implements ConsumerNetworkClient.PollCondition {
    // 保存了监听器的队列
    private final ConcurrentLinkedQueue<RequestFutureListener<T>> listeners = new ConcurrentLinkedQueue<>();
    
    public void addListener(RequestFutureListener<T> listener) {
        // 将监听器保存到队列里
        this.listeners.add(listener);
        
        if (failed())
            // 如果结果已经完成，并且结果失败，则执行回调函数
            fireFailure();
        else if (succeeded())
            // 如果结果已经完成，并且结果成功，则执行回调函数
            fireSuccess();
    }
    
    private void fireSuccess() {
        // 获取值
        T value = value();
        // 从队列里取出监听器，依次执行onSuccess回调
        while (true) {
            RequestFutureListener<T> listener = listeners.poll();
            if (listener == null)
                break;
            listener.onSuccess(value);
        }
    }

    private void fireFailure() {
        // 获取异常，
        RuntimeException exception = exception();
        // 从队列里取出监听器，依次执行onFailure回调
        while (true) {
            RequestFutureListener<T> listener = listeners.poll();
            if (listener == null)
                break;
            listener.onFailure(exception);
        }
    }
}
```



### 设置结果

RequestFuture提供了两个方法设置结果，一个是成功完成，调用complete方法设置结果。另一个是执行失败，调用raise方法设置异常。

```java
public void complete(T value) {
    try {
        // value必须为值，不能为异常
        if (value instanceof RuntimeException)
            throw new IllegalArgumentException("The argument to complete can not be an instance of RuntimeException");
        // 检查result的值是否为INCOMPLETE_SENTINEL，并且设置为value
        // 否则认为result的值已经被设置过了
        if (!result.compareAndSet(INCOMPLETE_SENTINEL, value))
            throw new IllegalStateException("Invalid attempt to complete a request future which is already complete");
        // 执行监听器
        fireSuccess();
    } finally {
        // 调用completedLatch的countDown方法，通知等待线程
        completedLatch.countDown();
    }
}

public void raise(RuntimeException e) {
    try {
        // 异常不能为空
        if (e == null)
            throw new IllegalArgumentException("The exception passed to raise must not be null");
        // 检查result的值是否为INCOMPLETE_SENTINEL，并且设置为异常
        if (!result.compareAndSet(INCOMPLETE_SENTINEL, e))
            throw new IllegalStateException("Invalid attempt to complete a request future which is already complete");
        // 执行监听器
        fireFailure();
    } finally {
        // 调用completedLatch的countDown方法，通知等待线程
        completedLatch.countDown();
    }
}
```



### 装饰 RequestFuture

RequestFuture有两个方法比较特殊，

chain方法，负责连接一个RequestFuture。当父RequestFuture完成时，子RequestFuture也同时完成。这里需要注意到两个RequestFuture的泛型必须相同。

```java
public void chain(final RequestFuture<T> future) {
    // 添加监听器来实现
    addListener(new RequestFutureListener<T>() {
        @Override
        public void onSuccess(T value) {
            // 当父RequestFuture完成时，会调用这个监听器的onSuccess方法
            // 这个监听器执行时，会调用future的complete方法设置结果
            future.complete(value);
        }

        @Override
        public void onFailure(RuntimeException e) {
            // 当父RequestFuture完成时，会调用这个监听器的onFailure方法
            // 这个监听器执行时，会调用future的raise方法设置结果
            future.raise(e);
        }
    });
}
```



compose方法，负责转换RequestFuture的泛型。比如一个RequestFuture<String> 类型，表示它的结果是String类型的。现在需要将结果转换为Integer类型，返回RequestFuture<Integer> ，那么就需要compose方法。

```java
// T 表示源类型，S表示目标类型
public <S> RequestFuture<S> compose(final RequestFutureAdapter<T, S> adapter) {
    // 这里生成S类型的RequestFuture
    final RequestFuture<S> adapted = new RequestFuture<>();
    // 添加监听器
    addListener(new RequestFutureListener<T>() {
        @Override
        public void onSuccess(T value) {
            // 当父RequestFuture完成时，会调用这个监听器的onSuccess方法
            // 这个监听器调用了RequestFutureAdapter的onSucceess方法，
            // RequestFutureAdapter这里需要设置adapted的结果
            adapter.onSuccess(value, adapted);
        }

        @Override
        public void onFailure(RuntimeException e) {
            adapter.onFailure(e, adapted);
        }
    });
    return adapted;
}
```



这里注意下传递的RequestFutureAdapter参数。RequestFutureAdapter它是一个接口，用户必须实现它的onSuccess和onFailure方法。

onSuccess方法，必须根据value参数，生成结果，并且赋予给S类型的RequestFuture。

onFailure方法，必须根据exception参数，生成结果，并且赋予给S类型的RequestFuture。





## 构造请求

ConsumerNetworkClient发送请求，本质上是通过NetworkClient发送。注意到以前讲述NetworkClient的原理，它发送请求之前，是需要先构造ClientRequest的。

ConsumerNetworkClient在构建请求时，传递了包含回调函数的类。回调类由RequestFutureCompletionHandler表示，它实现了RequestCompletionHandler接口。

这里着重看下RequestFutureCompletionHandler的onComplete函数，当请求响应完成后，会调用onComplete方法。这里只是将自身保存在一个队列里，等待之后的统一调用。

```java
private class RequestFutureCompletionHandler implements RequestCompletionHandler {
    private ClientResponse response;
    
    @Override
    public void onComplete(ClientResponse response) {
        // 更新响应数据
        this.response = response;
        // 将自身添加到pendingCompletion队列里
        pendingCompletion.add(this);
    }
}
```



ConsumerNetworkClient的send方法，负责构建请求，然后将请求保存到UnsentRequests集合里。UnsentRequests为每个节点都保存了一个请求队列。

```java
public class ConsumerNetworkClient implements Closeable {
    // NetworkClient 实例
    private final KafkaClient client;
    // ClientRequest集合，它为每个节点保存了一个请求队列
    private final UnsentRequests unsent = new UnsentRequests();

    public RequestFuture<ClientResponse> send(Node node,
                                              AbstractRequest.Builder<?> requestBuilder,
                                              int requestTimeoutMs) {
        long now = time.milliseconds();
        // 实例化RequestFutureCompletionHandler，里面实例化了RequestFuture
        RequestFutureCompletionHandler completionHandler = new RequestFutureCompletionHandler();
        // 构建ClientRequest，需要注意的是completionHandler，它作为回调函数
        ClientRequest clientRequest = client.newClientRequest(node.idString(), requestBuilder, now, true,
                requestTimeoutMs, completionHandler);
        // 将请求保存到unsent集合里
        unsent.put(node, clientRequest);

        // 如果NetworkClient阻塞了，通知它
        client.wakeup();
        // 返回 RequestFuture
        return completionHandler.future;
    }
}
```



## 处理响应

send方法只是将请求保存到了队列里，并没有发送。ConsumerNetworkClient还提供了poll方法，可以接收RequestFuture参数，等待请求完成。ConsumerNetworkClient放入poll方法，不仅仅负责发送请求，还包括处理响应。

```java
public class ConsumerNetworkClient implements Closeable {
    
     public void poll(RequestFuture<?> future) {
         // 循环执行poll方法，直到future完成
        while (!future.isDone())
            poll(Long.MAX_VALUE, time.milliseconds(), future);
    }
    
    public void poll(long timeout, long now, PollCondition pollCondition) {
        poll(timeout, now, pollCondition, false);
    }
    
    public void poll(long timeout, long now, PollCondition pollCondition, boolean disableWakeup) {
        // 执行完成的回调函数
        firePendingCompletedRequests();
        lock.lock();
        try {
            handlePendingDisconnects();
            // 发送请求
            long pollDelayMs = trySend(now);
            timeout = Math.min(timeout, pollDelayMs);
            if (pendingCompletion.isEmpty() && (pollCondition == null || pollCondition.shouldBlock())) {
                if (client.inFlightRequestCount() == 0)
                    timeout = Math.min(timeout, retryBackoffMs);
                client.poll(Math.min(maxPollTimeoutMs, timeout), now);
                now = time.milliseconds();
            } else {
                client.poll(0, now);
            }
            checkDisconnects(now);
            if (!disableWakeup) {
                maybeTriggerWakeup();
            }
            maybeThrowInterruptException();
            // 发送请求
            trySend(now);

            // 处理过期的请求
            failExpiredRequests(now);

            // 清空unset集合，因为请求都已经发送出去了
            unsent.clean();
        } finally {
            lock.unlock();0.
        }
        // 执行完成的回调函数
        firePendingCompletedRequests();
    }
}
```

trySend方法负责发送请求。它会遍历请求集合unsent，依次发送。

```java
private long trySend(long now) {
    long pollDelayMs = Long.MAX_VALUE;
    // 遍历unsent请求
    for (Node node : unsent.nodes()) {
        // 提取该节点的请求
        Iterator<ClientRequest> iterator = unsent.requestIterator(node);
        if (iterator.hasNext())
            pollDelayMs = Math.min(pollDelayMs, client.pollDelayMs(node, now));
        
        while (iterator.hasNext()) {
            // 遍历该节点的请求
            ClientRequest request = iterator.next();
            // 调用ready创建连接
            if (client.ready(node, now)) {
                // 调用NetworkClient的send方法发送请求
                client.send(request, now);
                // 移除掉已发送的请求
                iterator.remove();
            }
        }
    }
    return pollDelayMs;
}
```



firePendingCompletedRequests方法负责处理响应。我们回想下，在构建请求时，指定了回调类。回调函数执行时，会将自身添加到pendingCompletion队列里。

```java
public class ConsumerNetworkClient implements Closeable {
    // 请求完成的回调队列
    private final ConcurrentLinkedQueue<RequestFutureCompletionHandler> pendingCompletion = new ConcurrentLinkedQueue<>();
    
    private void firePendingCompletedRequests() {
        boolean completedRequestsFired = false;
        for (;;) {
            // 循环从队列中，提取completionHandler
            RequestFutureCompletionHandler completionHandler = pendingCompletion.poll();
            if (completionHandler == null)
                break;
            // 执行回调函数
            completionHandler.fireCompletion();
        }
}
```

最后看看RequestFutureCompletionHandler的fireCompletion方法，它会根据响应结果，来设置RequestFuture的结果。

```java
private class RequestFutureCompletionHandler implements RequestCompletionHandler {
    
    private final RequestFuture<ClientResponse> future;
    // 响应数据
    private ClientResponse response;
    // 如果请求失败，这里会设置异常值
    private RuntimeException e;
    
    public void fireCompletion() {
        if (e != null) {
            // 如果请求出现异常，则调用raise方法设置异常，完成future
            future.raise(e);
        } else if (response.versionMismatch() != null) {
            // 响应出现版本不支持错误
            future.raise(response.versionMismatch());
        } else if (....) {
            // 检查其他错误
            .....
        } else {
            // 调用complete方法，设置future的结果
            future.complete(response);
        }
    }
}
```

