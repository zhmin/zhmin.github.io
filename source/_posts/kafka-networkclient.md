---
title: Kafka NetworkClient 原理
date: 2019-03-06 00:53:10
tags: kafka
---

# Kafka NetworkClient 原理



Kafka的所有消息，都是通过NetworkClient发送消息。无论是Kafka的生产者，还是Kakfa的消费者，都会包含NetworkClient，才能将请求发送出去。

客户使用NetworkClient的方法是

1. 调用NetworkClient的ready方法，连接服务端
2. 调用NetworkClient的poll方法，处理连接
3. 调用NetworkClient的newClientRequest方法，创建请求ClientRequest
4. 然后调用NetworkClient的send方法，发送请求
5. 最后调用NetworkClient的poll方法，处理响应

![kafka networkclient](kafka-networkclient-flow.svg)

一条完整的请求分为下列几个阶段

1. 创建连接过程，这个过程包括建立底层的socket连接，和查询客户端的版本与服务端版本是否支持
2. 创建请求过程
3. 发送响应过程，这个过程包括请求序列化，网络发送
4. 处理响应过程



## 创建连接过程

NetworkClient发送请求之前，都需要先和服务端创建连接。NetworkClient负责管理与集群的所有连接。

```java
public class NetworkClient implements KafkaClient {
    
    // Kafka的Selector，用来异步发送网络数据
    private final Selectable selector;  
    // 保存与每个节点的连接状态
    private final ClusterConnectionStates connectionStates;

    public boolean ready(Node node, long now) {
        if (node.isEmpty())
            throw new IllegalArgumentException("Cannot connect to empty node " + node);
        // 判断是否允许发送请求
        if (isReady(node, now))
            return true;

        if (connectionStates.canConnect(node.idString(), now))
            initiateConnect(node, now);

        return false;
    }

    @Override
    public boolean isReady(Node node, long now) {
        // 当发现正在更新元数据时，会禁止发送请求。因为有可能集群的节点挂了，只有获取完元数据才能知道
        // 当连接没有创建完毕或者当前发送的请求过多时，也会禁止发送请求
        return !metadataUpdater.isUpdateDue(now) && canSendRequest(node.idString());
    }    
    
    // 检测连接状态，检测发送请求是否过多
    private boolean canSendRequest(String node) {
        return connectionStates.isReady(node) && selector.isChannelReady(node) && inFlightRequests.canSendMore(node);
    }
    
    // 创建连接
    private void initiateConnect(Node node, long now) {
        String nodeConnectionId = node.idString();
        try {
            // 更新连接状态为正在连接
            this.connectionStates.connecting(nodeConnectionId, now);
            // 调用selector异步连接
            selector.connect(nodeConnectionId,
                             new InetSocketAddress(node.host(), node.port()),
                             this.socketSendBuffer,
                             this.socketReceiveBuffer);
        } catch (IOException e) {
            /* attempt failed, we'll try again after the backoff */
            connectionStates.disconnected(nodeConnectionId, now);
            /* maybe the problem is our metadata, update it */
            metadataUpdater.requestUpdate();
        }
    }
}
```



上面讲述了如何建立socket连接，当socket连接建立后，NetworkClient还需要请求服务端的版本号。

poll 方法会调用 handleConnections处理连接，并且会创建版本请求。

```java
public class NetworkClient implements KafkaClient {
    
    // Kafka的Selector，用来异步发送网络数据
    private final Selectable selector;    
    // 是否需要与服务器的版本协调，默认都为true
    private final boolean discoverBrokerVersions;
    // 存储着要发送的版本请求，Key为主机地址，Value为构建请求的Builder
    private final Map<String, ApiVersionsRequest.Builder> nodesNeedingApiVersionsFetch = new HashMap<>();
    
    // 处理连接
    private void handleConnections() {
        // 遍历刚创建完成的连接
        for (String node : this.selector.connected()) {
            if (discoverBrokerVersions) {
                // 更新连接的状态为版本协调状态
                this.connectionStates.checkingApiVersions(node);
                // 将请求保存到nodesNeedingApiVersionsFetch集合里
                nodesNeedingApiVersionsFetch.put(node, new ApiVersionsRequest.Builder());
            } else {
                this.connectionStates.ready(node);
            }
        }
    }
}
```

创建完版本请求，接下来看看是如何发送请求和处理响应的。poll方法会调用handleInitiateApiVersionRequests发送版本协调请求，然后调用handleApiVersionsResponse负责处理响应。

```java
// 发送版本协调请求
private void handleInitiateApiVersionRequests(long now) {
    // 遍历请求集合nodesNeedingApiVersionsFetch
    Iterator<Map.Entry<String, ApiVersionsRequest.Builder>> iter = nodesNeedingApiVersionsFetch.entrySet().iterator();
    while (iter.hasNext()) {
        Map.Entry<String, ApiVersionsRequest.Builder> entry = iter.next();
        String node = entry.getKey();
        // 判断是否允许发送请求
        if (selector.isChannelReady(node) && inFlightRequests.canSendMore(node)) {
            ApiVersionsRequest.Builder apiVersionRequestBuilder = entry.getValue();
            // 调用newClientRequest生成请求
            ClientRequest clientRequest = newClientRequest(node, apiVersionRequestBuilder, now, true);
            // 发送请求
            doSend(clientRequest, true, now);
            iter.remove();
        }
    }
}

// 处理版本协调响应
private void handleApiVersionsResponse(List<ClientResponse> responses,
                                       InFlightRequest req, long now, ApiVersionsResponse apiVersionsResponse) {
    final String node = req.destination;
    // 判断响应和版本是否协调
    if (apiVersionsResponse.error() != Errors.NONE) {
        // 处理响应异常
        ......
    }
    // 更新连接状态为ready，表示可以发送正式请求了
    this.connectionStates.ready(node);

}
    
```



## 生成请求过程

NetworkClient使用ClientRequest类表示请求，它只是一些属性的集合。

```java
public final class ClientRequest {
    // 主机地址
    private final String destination;
    // 请求体的builder
    private final AbstractRequest.Builder<?> requestBuilder;
    // 请求头的correlation id
    private final int correlationId;
    // 请求头的client id
    private final String clientId;
    // 创建时间
    private final long createdTimeMs;
    // 是否需要响应
    private final boolean expectResponse;
    // 回调函数，用于处理响应
    private final RequestCompletionHandler callback;
}
```

NetworkClient的newClientRequest方法负责生成ClientRequest，它的原理只是简单的实例化ClientRequest

```java
public class NetworkClient implements KafkaClient {
    // nodeId是请求地址，requestBuilder是请求体的构造实例
    // createdTimeMs是请求创建时间，expectResponse表示是否需要响应
    // callback是处理响应的回调函数
    public ClientRequest newClientRequest(String nodeId, AbstractRequest.Builder<?> requestBuilder, long createdTimeMs, boolean expectResponse, RequestCompletionHandler callback) {
        return new ClientRequest(nodeId, requestBuilder, correlation++, clientId, createdTimeMs, expectResponse, callback);
    }
}
```



## 发送请求过程

NetworkClient创建完ClientRequest，会将它序列化，通过Selector异步发送出去，并且将请求封装成InFlightRequest，保存到队列InFlightRequests里。它的send方法定义如下

```java
public class NetworkClient implements KafkaClient {
    // 请求队列，保存正在发送但还没有收到响应的请求
    private final InFlightRequests inFlightRequests;
    
    private final List<ClientResponse> abortedSends = new LinkedList<>();
    
    // 发送请求
    public void send(ClientRequest request, long now) {
        doSend(request, false, now);
    }
    
    // 检测请求版本是否支持，如果支持则发送请求
    private void doSend(ClientRequest clientRequest, boolean isInternalRequest, long now) {
        AbstractRequest.Builder<?> builder = clientRequest.requestBuilder();
        try {
            // 检测版本
            NodeApiVersions versionInfo = apiVersions.get(nodeId);
            .....
            // 调用doSend方法
            doSend(clientRequest, isInternalRequest, now, builder.build(version));
        } catch (UnsupportedVersionException e) {
            // 请求的版本不协调，那么生成clientResponse，添加到abortedSends集合里
            ClientResponse clientResponse = new ClientResponse(clientRequest.makeHeader(builder.latestAllowedVersion()),
                    clientRequest.callback(), clientRequest.destination(), now, now,
                    false, e, null);
            abortedSends.add(clientResponse);
        }
    }
    
    // isInternalRequest表示发送前是否需要验证连接状态，如果为true则表示使用者已经确定连接是好的
    // request表示请求体
    private void doSend(ClientRequest clientRequest, boolean isInternalRequest, long now, AbstractRequest request) {
        String nodeId = clientRequest.destination();
        // 生成请求头
        RequestHeader header = clientRequest.makeHeader(request.version());
        // 结合请求头和请求体，序列化数据，保存到NetworkSend
        Send send = request.toSend(nodeId, header);
        // 生成InFlightRequest实例，它保存了发送前的所有信息
        InFlightRequest inFlightRequest = new InFlightRequest(
                header,
                clientRequest.createdTimeMs(),
                clientRequest.destination(),
                clientRequest.callback(),
                clientRequest.expectResponse(),
                isInternalRequest,
                request,
                send,
                now);
        // 添加到inFlightRequests集合里
        this.inFlightRequests.add(inFlightRequest);
        // 调用Selector异步发送数据
        selector.send(inFlightRequest.send);
    }   
}
```



InFlightRequest表示正在发送的请求，它存储着请求发送前的所有的信息。

不仅如此，它还支持生成响应ClientResponse。当正常收到响应时，completed 方法会根据响应内容生成ClientResponse。当连接突然断开，disconnected方法会生成ClientResponse。

```java
static class InFlightRequest {
    // 请求体
    final RequestHeader header;
    // 请求地址
    final String destination;
    // 回调函数
    final RequestCompletionHandler callback;
    // 是否需要服务端返回响应
    final boolean expectResponse;
    // 请求体
    final AbstractRequest request;
    // 表示发送前是否需要验证连接状态
    final boolean isInternalRequest; 
    // 请求的序列化数据
    final Send send;
    // 发送时间
    final long sendTimeMs;
    // 请求的创建时间，这个是ClientRequest的创建时间
    final long createdTimeMs;

    public ClientResponse completed(AbstractResponse response, long timeMs) {
        return new ClientResponse(header, callback, destination, createdTimeMs, timeMs, false, null, response);
    }

    public ClientResponse disconnected(long timeMs) {
        return new ClientResponse(header, callback, destination, createdTimeMs, timeMs, true, null, null);
    }
}
```



接下来看看集合InFlightRequests，它为每个连接生成一条队列，存储着已发送但还未收到响应的请求。根据队列的长度，它能控制发送的速度。它的原理比较简单，这里介绍下它的几个重要方法

```java
final class InFlightRequests {
    // 最大的请求数目
    private final int maxInFlightRequestsPerConnection;
    // InFlightRequest集合，Key为主机地址，Value为请求队列
    private final Map<String, Deque<NetworkClient.InFlightRequest>> requests = new HashMap<>();
    
    // 将请求添加到队列头部
    public void add(NetworkClient.InFlightRequest request);
    
    // 取出该连接，最老的请求
    public NetworkClient.InFlightRequest completeNext(String node);
    
    // 取出该连接，最新的请求
    public NetworkClient.InFlightRequest completeLastSent(String node);
        
    // 判断是否该连接能发送请求
    public boolean canSendMore(String node) {
        Deque<NetworkClient.InFlightRequest> queue = requests.get(node);
        return queue == null || queue.isEmpty() ||
            // 必须等待前面的请求发送完毕，
            // 并且没有响应的请求数目小于指定数目
               (queue.peekFirst().send.completed() && queue.size() < this.maxInFlightRequestsPerConnection);
    }    
}
```



## 处理响应过程

### 请求发送完成

当请求发送完成后，会触发handleCompletedSends函数，处理那些不需要响应的请求。

```java
private void handleCompletedSends(List<ClientResponse> responses, long now) {
    // 遍历发送完成的请求，通过调用Selector获得自从上一次poll开始的请求
    for (Send send : this.selector.completedSends()) {
        // 从队列中取出最新的请求
        InFlightRequest request = this.inFlightRequests.lastSent(send.destination());
        if (!request.expectResponse) {
            // 如果这个请求不要求响应，则提取最新的请求
            this.inFlightRequests.completeLastSent(send.destination());
            // 调用completed方法生成ClientResponse
            responses.add(request.completed(null, now));
        }
    }
}
```

这里可能有点疑惑，怎么能保证从Selector返回的请求，是对应到队列中最新的请求。仔细想一下，每个请求发送，都要等待前面的请求发送完成，这样就能保证同一时间只有一个请求正在发送   。因为Selector返回的请求，是从上一次poll开始的，所以这样就能够保证。

### 请求收到响应

当请求收到响应后，会触发handleCompletedReceives函数，处理响应。

```java
private void handleCompletedReceives(List<ClientResponse> responses, long now) {
    // 遍历响应，通过Selector返回未处理的响应
    for (NetworkReceive receive : this.selector.completedReceives()) {
        
        String source = receive.source();
        // 提取最老的请求
        InFlightRequest req = inFlightRequests.completeNext(source);
        // 解析响应，并且验证响应头，生成Struct实例
        Struct responseStruct = parseStructMaybeUpdateThrottleTimeMetrics(receive.payload(), req.header,
            throttleTimeSensor, now);
        // 生成响应体
        AbstractResponse body = AbstractResponse.parseResponse(req.header.apiKey(), responseStruct);
        // 处理元数据请求响应
        if (req.isInternalRequest && body instanceof MetadataResponse)
            metadataUpdater.handleCompletedMetadataResponse(req.header, now, (MetadataResponse) body);
        else if (req.isInternalRequest && body instanceof ApiVersionsResponse)
            // 处理版本协调响应
            handleApiVersionsResponse(responses, req, now, (ApiVersionsResponse) body);
        else
            // 生成ClientResponse添加到列表中
            responses.add(req.completed(body, now));
    }
}

private static Struct parseStructMaybeUpdateThrottleTimeMetrics(ByteBuffer responseBuffer, RequestHeader requestHeader, Sensor throttleTimeSensor, long now) {
    // 解析响应头
    ResponseHeader responseHeader = ResponseHeader.parse(responseBuffer);
    // 解析响应体
    Struct responseBody = requestHeader.apiKey().parseResponse(requestHeader.apiVersion(), responseBuffer);
    // 验证请求头与响应头的 correlation id 必须相等
    correlate(requestHeader, responseHeader);
    return responseBody;
}
```



### 执行处理响应函数

NetworkClient的 poll 方法，会将已完成的请求，生成ClientReponse收集起来，然后逐一执行它的回调函数。

```java
public List<ClientResponse> poll(long timeout, long now) {
    long updatedNow = this.time.milliseconds();
    // 响应列表
    List<ClientResponse> responses = new ArrayList<>();
    // 处理各种情况，生成响应，添加到列表中
    handleCompletedSends(responses, updatedNow);
    handleCompletedReceives(responses, updatedNow);
    handleDisconnections(responses, updatedNow);
    handleConnections();
    handleInitiateApiVersionRequests(updatedNow);
    handleTimedOutRequests(responses, updatedNow);
    // 执行处理响应函数
    completeResponses(responses);

    return responses;
}
```

在InFlightRequest类中，我们已经看到了它是如何生成ClientReponse实例的。首先看看ClientReponse的几个重要属性和方法

```java
public class ClientResponse {
    
    // 回调函数，由ClientRequest指定
    private final RequestCompletionHandler callback;
    // 响应体
    private final AbstractResponse responseBody;
    
    // 响应完成的回调函数
    public void onComplete() {
        // 调用RequestCompletionHandler回调
        if (callback != null)
            callback.onComplete(this);
    }    
```

下面继续看看NetworkClient的completeResponses方法，这里依次执行了每个ClientResponse的onComplete回调方法

```java
private void completeResponses(List<ClientResponse> responses) {
    for (ClientResponse response : responses) {
        try {
            response.onComplete();
        } catch (Exception e) {
            log.error("Uncaught error in request completion:", e);
        }
    }
}
```

