---
title: Kafka 协议格式
date: 2019-03-04 23:49:00
tags:
categories: kafka
---

# Kafka 协议格式

Kafka通常作为一个集群存在，集群内部会有着很多的通信，包括客户端与服务端的通信，服务端内部的通信。这些通信都有着固定的协议格式，通过了解这些协议格式，可以使我们对Kafka的原理有着更好的理解。本篇文章主要介绍这些协议的基本原理，包括如何定义格式，如何序列化数据。

# 定义协议格式

Kafka对于每一种协议，都有着自己固定的格式。它由固定的字段组成，并且这些字段按照顺序存储，并且有着自己的类型和名称。协议格式由Schema类表示，字段由Field类型。

### 字段定义

每个字段都有着数据类型，字段名称。目前数据类型支持常见的Int和String等类型。字段类型由Type类表示，它还负责如何序列化数值。

以String类型为例，它继承Type基类，注意到write和read方法，对应于数据的写入和读取。

```java
    public static final Type STRING = new Type() {
         
        @Override
        public void write(ByteBuffer buffer, Object o) {
            // 将string按照utf8编码，生成byte数组
            byte[] bytes = Utils.utf8((String) o);
            // 因为只有2个字节表示string长度，所以需要检查string长度是否超过最大值
            if (bytes.length > Short.MAX_VALUE)
                throw new SchemaException("String length " + bytes.length + " is larger than the maximum string length.");
            // 写入string长度
            buffer.putShort((short) bytes.length);
            // 写入数据
            buffer.put(bytes);
        }

        @Override
        public String read(ByteBuffer buffer) {
            // 读取string长度
            short length = buffer.getShort();
            // 检测数据长度
            if (length < 0)
                throw new SchemaException("String length " + length + " cannot be negative");
            if (length > buffer.remaining())
                throw new SchemaException("Error reading string of length " + length + ", only " + buffer.remaining() + " bytes available");
            // 读取字符串
            String result = Utils.utf8(buffer, length);
            // 移动buffer的读取位置
            buffer.position(buffer.position() + length);
            return result;
        }

        @Override
        public int sizeOf(Object o) {
            // 返回整个字段的长度
            return 2 + Utils.utf8Length((String) o);
        }
    };
```



字段都是由Field的基类表示，每个字段都对应着一个Type。Field的原理也很简单，它没有方法，只是包含了一些属性

```java
public class Field {
    // 字段名称
    public final String name;
    // 字段注释
    public final String docString;
    // 字段类型
    public final Type type;
    // 是否有默认值
    public final boolean hasDefaultValue;
    // 默认值
    public final Object defaultValue;
}
```

以Int32字段为例，它指定了字段类型为Type.INT32

```java
public static class Int32 extends Field {
    public Int32(String name, String docString) {
        super(name, Type.INT32, docString, false, null);
    }

    public Int32(String name, String docString, int defaultValue) {
        super(name, Type.INT32, docString, true, defaultValue);
    }
}
```



这里还需介绍一个特殊的字段类型，它表示数组。

```java
public class ArrayOf extends Type {
    // 数组元素的类型
    private final Type type;
    // 是否允许数组为空
    private final boolean nullable;

    @Override
    public void write(ByteBuffer buffer, Object o) {
        // 如果值为null，那么数组长度为-1表示这种情况
        if (o == null) {
            buffer.putInt(-1);
            return;
        }
        Object[] objs = (Object[]) o;
        // 获取数组长度，并且写入
        int size = objs.length;
        buffer.putInt(size);
        // 遍历数组，依次将元素写入buffer
        for (Object obj : objs)
            type.write(buffer, obj);
    }

    @Override
    public Object read(ByteBuffer buffer) {
        // 读取数据长度
        int size = buffer.getInt();
        // 检测数组是否为空
        if (size < 0 && isNullable())
            return null;
        else if (size < 0)
            throw new SchemaException("Array size " + size + " cannot be negative");
        // 检测数据的长度，这里只是粗略的检测，因为一个元素至少是一个字节(这种情况表示数据类型为Int8)
        if (size > buffer.remaining())
            throw new SchemaException("Error reading array of size " + size + ", only " + buffer.remaining() + " bytes available");
        // 依次读取数据，生成数组
        Object[] objs = new Object[size];
        for (int i = 0; i < size; i++)
            objs[i] = type.read(buffer);
        return objs;
    }
}
```



### 格式定义

上面讲完了字段，协议的格式也是由这些字段组成。Schema中的字段，使用了BoundField类表示。其实BoundField只是简单包含了Field，表示这个字段属于某个Schema。

```java
public class Schema extends Type {
    // 字段列表
    private final BoundField[] fields;
    // 可以根据名称找到字段
    private final Map<String, BoundField> fieldsByName;

    public Schema(Field... fs) {
        this.fields = new BoundField[fs.length];
        this.fieldsByName = new HashMap<>();
        for (int i = 0; i < this.fields.length; i++) {
            Field def = fs[i];
            if (fieldsByName.containsKey(def.name))
                throw new SchemaException("Schema contains a duplicate field: " + def.name);
            // 实例化BoundField
            this.fields[i] = new BoundField(def, this, i);
            this.fieldsByName.put(def.name, this.fields[i]);
        }
    }
}
```

上面注意到Schema也是Type的子类，说明Schema也是一种类型。既然Schema包含了多个字段，而且这些字段的类型可以是Schema，那么表示Schema也可以嵌套了。

Schema的数据读写原理很简单，就是按照字段的顺序依次读写。



## 数据实例

上面讲述了Schema是如何协议格式的，那么一个具体的协议数据是由Struct类表示。它的每个字段的值，按照顺序存储到Object数组中。

注意到两个特殊的字段类型，如果字段是Schema类型，那么对应的Object值是Struct实例。如果是数组类型，那么对于的Object值是Struct数组。

```java
public class Struct {
    private final Schema schema;
    private final Object[] values;
}
```



## 请求协议

Kafka的所有请求也有着自己固定的格式，但是这些请求也有部分数据的格式是相同的，Kafka将这部分数据提取为请求头。所以一个完成的Kafka请求是由请求头和请求体两部分组成的。下面首先来看看请求头。



### 请求头格式

在kafka的请求中，有一个请求比较特殊，是关闭服务的请求。Kafka单独为它设置了一个请求头。其余的请求都共用一个请求头，由RequestHeader的SCHEMA实例表示。

```java
public class RequestHeader extends AbstractRequestResponse {
    
    private static final String API_KEY_FIELD_NAME = "api_key";
    private static final String API_VERSION_FIELD_NAME = "api_version";
    private static final String CLIENT_ID_FIELD_NAME = "client_id";
    private static final String CORRELATION_ID_FIELD_NAME = "correlation_id";

    public static final Schema SCHEMA = new Schema(
            new Field(API_KEY_FIELD_NAME, INT16, "The id of the request type."),
            new Field(API_VERSION_FIELD_NAME, INT16, "The version of the API."),
            new Field(CORRELATION_ID_FIELD_NAME, INT32, "A user-supplied integer value that will be passed back with the response"),
            new Field(CLIENT_ID_FIELD_NAME, NULLABLE_STRING, "A user specified identifier for the client making the request.", ""));
}
```

它有四个字段，含义如下：

- api_key， 请求类型 id
- api_version，请求版本
- correlation_id， 序号，有客户端生成，服务器发送响应会携带这个序号
- client_id，客户端 id，指明由哪个客户发送请求的



### 请求体

Kafka的每个请求对应的请求体格式都不一样，每个请求都包含了一个内部类Builder，用来构建请求体。

Kafka的所有请求都会继承AbstractRequest类，并且在内部还会有继承AbstractRequest的Builder类的子类。

```java
public abstract class AbstractRequest extends AbstractRequestResponse {
    
    // 构建该请求的Builder
    public static abstract class Builder<T extends AbstractRequest> {
        // ApiKeys是枚举类，表示请求类型
        private final ApiKeys apiKey;
        // 请求版本的要求
        private final short oldestAllowedVersion;
        private final short latestAllowedVersion;
        
        //子类需要实现buidl方法，构造请求
        public abstract T build(short version);
    }
    
    // 子类需要实现toStruct方法，才能利用Struct序列化请求
    protected abstract Struct toStruct();
}    
```

我们以ProduceRequest为例，它表示向kafka添加数据的请求。它的请求格式比较复杂，最新版的格式，如下图所示

![produce-request](kafka-protocol-produce-request.svg)

```java
public class ProduceRequest extends AbstractRequest {
    private static final String ACKS_KEY_NAME = "acks";
    private static final String TIMEOUT_KEY_NAME = "timeout";
    private static final String TOPIC_DATA_KEY_NAME = "topic_data";

    // topic level field names
    private static final String PARTITION_DATA_KEY_NAME = "data";

    // partition level field names
    private static final String RECORD_SET_KEY_NAME = "record_set";

    // 每个topic的数据格式
    private static final Schema TOPIC_PRODUCE_DATA_V0 = new Schema(
            TOPIC_NAME,
            new Field(PARTITION_DATA_KEY_NAME, new ArrayOf(new Schema(
                    PARTITION_ID,
                    new Field(RECORD_SET_KEY_NAME, RECORDS)))));
    // 最新版的请求格式
    private static final Schema PRODUCE_REQUEST_V3 = new Schema(
            CommonFields.NULLABLE_TRANSACTIONAL_ID,
            new Field(ACKS_KEY_NAME, INT16, "..."),
            new Field(TIMEOUT_KEY_NAME, INT32, "The time to await a response in ms."),
            // TOPIC_PRODUCE_DATA_V0数组类型
            new Field(TOPIC_DATA_KEY_NAME, new ArrayOf(TOPIC_PRODUCE_DATA_V0)));                    }
          
```

继续看它的toStruct方法，ProduceRequest会从上往下一层层的生成Struct实例。遇到Schema类型的字段，则生成Struct实例。遇到数组类型的字段，则生成Object数组。

```java
@Override
public Struct toStruct() {
    // Store it in a local variable to protect against concurrent updates
    Map<TopicPartition, MemoryRecords> partitionRecords = partitionRecordsOrFail();
    short version = version();
    // requestSchema方法会根据版本，返回对应的Schema
    Struct struct = new Struct(ApiKeys.PRODUCE.requestSchema(version));
    
    Map<String, Map<Integer, MemoryRecords>> recordsByTopic = CollectionUtils.groupDataByTopic(partitionRecords);
    // 设置对应的属性
    struct.set(ACKS_KEY_NAME, acks);
    struct.set(TIMEOUT_KEY_NAME, timeout);
    struct.setIfExists(NULLABLE_TRANSACTIONAL_ID, transactionalId);

    List<Struct> topicDatas = new ArrayList<>(recordsByTopic.size());
    for (Map.Entry<String, Map<Integer, MemoryRecords>> topicEntry : recordsByTopic.entrySet()) {
        // 因为TOPIC_DATA_KEY_NAME字段是Schema类型的，这里调用了instance方法实例化Struct
        Struct topicData = struct.instance(TOPIC_DATA_KEY_NAME);
        
        topicData.set(TOPIC_NAME, topicEntry.getKey());
        List<Struct> partitionArray = new ArrayList<>();
        for (Map.Entry<Integer, MemoryRecords> partitionEntry : topicEntry.getValue().entrySet()) {
            MemoryRecords records = partitionEntry.getValue();
            // 因为PARTITION_DATA_KEY_NAME字段是Schema类型，这里调用了instance实例化Struct
            Struct part = topicData.instance(PARTITION_DATA_KEY_NAME)
                    .set(PARTITION_ID, partitionEntry.getKey())
                    .set(RECORD_SET_KEY_NAME, records);
            // 添加partition数据
            partitionArray.add(part);
        }
        // 设置PARTITION_DATA_KEY_NAME字段
        topicData.set(PARTITION_DATA_KEY_NAME, partitionArray.toArray());
        topicDatas.add(topicData);
    }
    // 设置topic字段的数据
    struct.set(TOPIC_DATA_KEY_NAME, topicDatas.toArray());
    return struct;
}
```

同样ProduceRequest内部也实现了Buidler类，它的build方法实例化并返回了ProduceRequest对象

```java
public class ProduceRequest extends AbstractRequest {
    public static class Builder extends AbstractRequest.Builder<ProduceRequest> {
        private final short acks;
        private final int timeout;
        private final Map<TopicPartition, MemoryRecords> partitionRecords;
        private final String transactionalId; 
        
        @Override
        public ProduceRequest build(short version) {
            return new ProduceRequest(version, acks, timeout, partitionRecords, transactionalId);
        }        
    }
}
```



### 序列化

Kafka通过Builder构建完请求体后，会将请求体一起序列化。AbstractRequest基类的serialize方法，会序列化请求。

```java
public abstract class AbstractRequest extends AbstractRequestResponse {
    
    public ByteBuffer serialize(RequestHeader header) {
        // 接收请求头，然后调用父类的serialize序列化请求
        return serialize(header.toStruct(), toStruct());
    }
}

public abstract class AbstractRequestResponse {
    // 将请求头和请求体，序列化到ByteBuffer
    public static ByteBuffer serialize(Struct headerStruct, Struct bodyStruct) {
        ByteBuffer buffer = ByteBuffer.allocate(headerStruct.sizeOf() + bodyStruct.sizeOf());
        headerStruct.writeTo(buffer);
        bodyStruct.writeTo(buffer);
        buffer.rewind();
        return buffer;
    }
}
```



## 响应协议

kafka的响应协议通请求协议一样，分为响应头部和响应体两部分。



### 响应头部

响应头部很简单，只是包含了从客户端发来的correlation_id

```java
public class ResponseHeader extends AbstractRequestResponse {
    public static final Schema SCHEMA = new Schema(
            new Field("correlation_id", INT32, "The user-supplied value passed in with the request"));
}
```



### 响应体

Kafka的响应体都是由AbstractResponse的子类，有下列重要方法

```java
public abstract class AbstractResponse extends AbstractRequestResponse {
    // 序列化并且保存到NetworkSend，后面会由网络发送出去
    protected Send toSend(String destination, ResponseHeader header, short apiVersion) {
        return new NetworkSend(destination, serialize(apiVersion, header));
    }
    
    // 接收响应头部，并且序列化到buffer
    public ByteBuffer serialize(short version, ResponseHeader responseHeader) {
        return serialize(responseHeader.toStruct(), toStruct(version));
    }
    
    // 转化为struct实例
    protected abstract Struct toStruct(short version);
}
    
```



以ProduceResponse响应为例，它的格式如下图所示：

![produce-response](kafka-protocol-produce-response.svg)

```java
public class ProduceResponse extends AbstractResponse {

    private static final String RESPONSES_KEY_NAME = "responses";
    private static final String PARTITION_RESPONSES_KEY_NAME = "partition_responses";
    private static final String BASE_OFFSET_KEY_NAME = "base_offset";
    private static final String LOG_APPEND_TIME_KEY_NAME = "log_append_time";
    
    private static final Schema PRODUCE_RESPONSE_V2 = new Schema(
            new Field(RESPONSES_KEY_NAME, new ArrayOf(new Schema(
                    TOPIC_NAME,
                    new Field(PARTITION_RESPONSES_KEY_NAME, new ArrayOf(new Schema(
                            PARTITION_ID,
                            ERROR_CODE,
                            new Field(BASE_OFFSET_KEY_NAME, INT64),
                            new Field(LOG_APPEND_TIME_KEY_NAME, INT64, "..."))))))),
            THROTTLE_TIME_MS);

    private static final Schema PRODUCE_RESPONSE_V3 = PRODUCE_RESPONSE_V2;
}
```



ProduceResponse的toStruct方法的原理，同ProduceRequest的原理相同。

ProduceResponse的序列化的原理，同ProduceRequest的原理相同。



## 总结

通过上面协议原理的了解，我们在分析请求和响应的时候，可以直接查看它们的格式。所有kafka的请求和响应，都定义在org.apache.kafka.common.requests包里面。