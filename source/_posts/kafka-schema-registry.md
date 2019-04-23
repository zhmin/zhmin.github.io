---
title: Kafka Schema Registry 原理
date: 2019-04-23 21:01:45
tags: kafka, schema, registry
categories: kafka
---

# Kafka Schema Registry 原理

Confluent 公司为了能让 Kafka 支持 Avro 序列化，创建了 Kafka Schema Registry 项目，项目地址为 <https://github.com/confluentinc/schema-registry> 。对于存储大量数据的 kafka 来说，使用 Avro 序列化，可以减少数据的存储空间提高了存储量，减少了序列化时间提高了性能。 Kafka 有多个topic，里面存储了不同种类的数据，每种数据都对应着一个 Avro schema 来描述这种格式。Registry 服务支持方便的管理这些 topic 的schema，它还对外提供了多个 restful 接口，用于存储和查找。



## Avro 序列化示例

Avro 序列化相比常见的序列化（比如 json）会更快，序列化的数据会更小。相比 protobuf ，它可以支持实时编译，不需要像 protobuf 那样先定义好数据格式文件，编译之后才能使用。下面简单的介绍下 如何使用 Avro 序列化：

数据格式文件：

```json
{
 "namespace": "example.avro",
 "type": "record",
 "name": "User",
 "fields": [
     {"name": "name", "type": "string"},
     {"name": "favorite_number",  "type": ["int", "null"]},
     {"name": "favorite_color", "type": ["string", "null"]}
 ]
}
```



序列化生成字节：

```java
// 解析数据格式文件  
Schema schema = new Schema.Parser().parse(new File("user.avsc"));
// 创建一个实例
GenericRecord user = new GenericData.Record(schema);
user.put("name", "Alyssa");
user.put("favorite_number", 256);

// 构建输出流，保存结果
ByteArrayOutputStream out = new ByteArrayOutputStream();
// BinaryEncoder负责向输出流，写入数据
BinaryEncoder encoder =  EncoderFactory.get().directBinaryEncoder(out, null);
// DatumWriter负责序列化
DatumWriter<GenericRecord> datumWriter = new GenericDatumWriter<GenericRecord>(schema);
// 调用DatumWriter序列化，并将结果写入到输出流
datumWriter.write(user, encoder);
// 刷新缓存
encoder.flush();

// 获取序列化的结果
byte[] result = out.toByteArray();
```



更多用法可以参见官方文档，<http://avro.apache.org/docs/current/gettingstartedjava.html>



## Kafka 客户端使用原理

Kafka Schema Registry 提供了 KafkaAvroSerializer 和 KafkaAvroDeserializer 两个类。Kafka 如果要使用 Avro 序列化， 在实例化 KafkaProducer 和 KafkaConsumer 时， 指定序列化或反序列化的配置。

客户端发送数据的流程图如下所示：

<img src="kafka-schema-registry.svg">

我们向 kafka 发送数据时，需要先向 Schema Registry 注册 schema，然后序列化发送到 kafka 里。当我们需要从 kafka 消费数据时，也需要先从 Schema Registry 获取 schema，然后才能解析数据。

下面以实例 KafkaProducer 的使用为例

```java
public class SchemaProducer {

    public static void main(String[] args) throws Exception {
        
        String kafkaHost = "xxx.xxx.xxx.xxx:9092";
        String topic = "schema-tutorial";
        String schameFilename = "user.json";
        String registryHost = "http://xxx.xxx.xxx.xxx:8081";

        Properties props = new Properties();
        props.put(ProducerConfig.BOOTSTRAP_SERVERS_CONFIG, kafkaHost);
        props.put(ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG, StringSerializer.class);
        // 指定Value的序列化类，KafkaAvroSerializer
        props.put(ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG, KafkaAvroSerializer.class);
        // 指定 registry 服务的地址
        // 如果 Schema Registry 启动了高可用，那么这儿的配置值可以是多个服务地址，以逗号隔开
        props.put("schema.registry.url", registryHost);
        KafkaProducer<String, GenericRecord> producer = new KafkaProducer<>(props);

        String key = "Alyssa key";
        Schema schema = new Schema.Parser().parse(new File(schameFilename));
        GenericRecord avroRecord = new GenericData.Record(schema);
        avroRecord.put("name", "Alyssa");
        avroRecord.put("favorite_number", 256);

        // 发送消息
        ProducerRecord<String, GenericRecord> record = new ProducerRecord<>(topic, key, avroRecord);
        producer.send(record);
        producer.flush();
        producer.close();
    }
}
```



上面使用到了 KafkaAvroSerializer 序列化消息，接下来看看 KafkaAvroSerializer 的 原理。我们知道 Kafka 的消息由 Key 和 Value 组成，这两部分的值可以有不同的数据格式。而这些数据格式都会保存在 Registry 服务端，客户端需要指定数据格式的名称（在 Registry 中叫做 subject），才能获取到。如果我们要获取当前消息 Key 这部分的数据格式，它对于的 subject 名称为 <topic>-key，如果要获取 Value 这部分的数据格式，它对应的 subject 名称为 <topic>-value（topic 为该消息所在的 topic 名称）。

 Kafka Schema Registry 还支持修改数据格式，这样对于同一个 topic ，它的消息有多个版本，前面的消息和最新的消息都可能会完全不一样，那么客户怎么区分呢。Registry 会为每种数据格式都会分配一个 id 号，然后发送的每条消息都会附带对应的数据格式 id。

KafkaProducer 在第一次序列化的时候，会自动向 Registry 服务端注册。服务端保存数据格式后，会返回一个 id 号。KafkaProducer发送消息的时候，需要附带这个 id 号。这样 KafkaConsumer 在读取消息的时候，通过这个 id 号，就可以从 Registry 服务端 获取。



Registry 客户端负责向服务端发送 http 请求，然后会将结果缓存起来，以提高性能。

```java
public class CachedSchemaRegistryClient implements SchemaRegistryClient {
  // Key 为数据格式的名称， 里面的 Value 为 Map类型，它对于的 Key 为数据格式，Value 为对应的 id 号
  private final Map<String, Map<Schema, Integer>> schemaCache;
  // Key 为数据格式的名称，里面的 Value 为 Map类型，它对于的 Key 为 id 号，Value 为对应的数据格式
  // 这个集合比较特殊，当 Key 为 null 时，表示 id 到 数据格式的缓存
  private final Map<String, Map<Integer, Schema>> idCache;
        
  @Override
  public synchronized int register(String subject, Schema schema, int version, int id)
      throws IOException, RestClientException {
    // 从schemaCache查找缓存，如果不存在则初始化空的哈希表
    final Map<Schema, Integer> schemaIdMap =
        schemaCache.computeIfAbsent(subject, k -> new HashMap<>());

    // 获取对应的 id 号
    final Integer cachedId = schemaIdMap.get(schema);
    if (cachedId != null) {
      // 检查 id 号是否有冲突
      if (id >= 0 && id != cachedId) {
        throw new IllegalStateException("Schema already registered with id "
            + cachedId + " instead of input id " + id);
      }
      // 返回缓存的 id 号
      return cachedId;
    }

    if (schemaIdMap.size() >= identityMapCapacity) {
      throw new IllegalStateException("Too many schema objects created for " + subject + "!");
    }
      
    // 如果缓存没有，则向服务端发送 http 请求 
    final int retrievedId = id >= 0
                            ? registerAndGetId(subject, schema, version, id)
                            : registerAndGetId(subject, schema);
    // 缓存结果
    schemaIdMap.put(schema, retrievedId);
    idCache.get(null).put(retrievedId, schema);
    return retrievedId;
  }
}    
```



## Registry 服务端



### 存储数据

Registry 服务端将数据格式存储到 Kafka 中，对应的 topic 名称为 _schemas。存储消息的格式如下：

- Key 部分，包含数据格式名称，版本号，由 SchemaRegistryKey 类表示。 
- Value部分，包含数据格式名称，版本号， 数据格式 id 号，数据格式的内容，是否被删除， 由 SchemaRegistryValue 类表示。



Registry 服务端在存储Kafka之前，还会将上述的 Key 和 Value 序列化，目前序列化由两种方式：

- json 序列化，由 ZkStringSerializer 类负责
- 将 SchemaRegistryKey 或 SchemaRegistryValue 强制转换为 String 类型保存起来



### 处理请求

Registry 服务端主要负责两种请求，注册数据格式 schema 请求和 获取数据格式 schema 请求。

如果 Registry 服务端启动了高可用，说明有多个服务端在运行。如果注册 schema 请求发送给了 follower，那么 follower 会将请求转发给 leader。至于获取 schema 请求，follower 和 leader 都能处理，因为 schema 最后都存在了 kafka 中，它们直接从 kafka 里读取。

处理注册 schema 请求

```java
public class KafkaSchemaRegistry implements SchemaRegistry, MasterAwareSchemaRegistry {

  public int registerOrForward(String subject,
                               Schema schema,
                               Map<String, String> headerProperties)
      throws SchemaRegistryException {
    // 检测这个schema是否之前注册过
    Schema existingSchema = lookUpSchemaUnderSubject(subject, schema, false);
    if (existingSchema != null) {
      if (schema.getId() != null && schema.getId() >= 0 && !schema.getId().equals(existingSchema.getId())
      ) {
        throw new IdDoesNotMatchException(existingSchema.getId(), schema.getId());
      }
      return existingSchema.getId();
    }

    synchronized (masterLock) {
      if (isMaster()) {
        // 如果是leader，那么执行register方法，写schema到kafka
        return register(subject, schema);
      } else {
        // 如果是follower，那么转发请求到 leader
        if (masterIdentity != null) {
          return forwardRegisterRequestToMaster(subject, schema, headerProperties);
        } else {
          throw new UnknownMasterException("Register schema request failed since master is "
                                           + "unknown");
        }
      }
    }
  }
}
```

上面调用了 register 方法保存 schema，同时它也为这个 schema 分配了一个 id。这里简单说下自增 id 生成器的算法，目前有两种实现方式。一种是基于内存的方式，自己维护一个计数器。另外一种是基于zookeeper的方式，每次从 zookeeper 获取一个 id 段，然后一个 id，一个 id 的分配出去。



处理获取 schema 请求

```java
public class KafkaSchemaRegistry implements SchemaRegistry, MasterAwareSchemaRegistry {
  // 负责读取kafka
  final KafkaStore<SchemaRegistryKey, SchemaRegistryValue> kafkaStore;
  // 缓存
  private final LookupCache<SchemaRegistryKey, SchemaRegistryValue> lookupCache;
    
  @Override
  public SchemaString get(int id) throws SchemaRegistryException {
    SchemaValue schema = null;
    try {
      // 从缓存中查找，根据 id 获取消息的key
      SchemaKey subjectVersionKey = lookupCache.schemaKeyById(id);
      if (subjectVersionKey == null) {
        return null;
      }
      // 从kafka中读取消息的value
      schema = (SchemaValue) kafkaStore.get(subjectVersionKey);
      if (schema == null) {
        return null;
      }
    } catch (StoreException e) {
      throw new SchemaRegistryStoreException(...);
    }
    // 返回结果
    SchemaString schemaString = new SchemaString();
    schemaString.setSchemaString(schema.getSchema());
    return schemaString;
  }
}   
```



### 高可用

如果要实现高可用，需要运行多个 Registry 服务，这些服务中必须选择出一个 leader，所有的请求都是最终 由 leader 来负责。当 leader 挂掉之后，就会触发选举操作，来选举出新的 leader。选举的实现有两种方式，基于kafka 和 基于 zookeeper。 

基于 kafka 的原理是利用消费组，因为消费组的每个成员都需要和 kafka coordinator 服务端保持心跳，如果有成员挂了，那么就会触发组的重分配操作。重分配操作会从存活的成员中，选出 leader 角色。

KafkaGroupMasterElector 启动了一个心跳线程，定期发送心跳请求。它 实现了监听器的接口，当出发开始选举时会调用onRevoked方法，当选举完之后会调用onAssigned方法。

```java
public class KafkaGroupMasterElector implements MasterElector, SchemaRegistryRebalanceListener {


  public void init() throws SchemaRegistryTimeoutException, SchemaRegistryStoreException {
    // 心跳线程
    executor = Executors.newSingleThreadExecutor();
    executor.submit(new Runnable() {
      @Override
      public void run() {
        try {
          while (!stopped.get()) {
            // 循环调用poll方法，处理心跳
            coordinator.poll(Integer.MAX_VALUE);
          }
        } catch (Throwable t) {
          log.error("Unexpected exception in schema registry group processing thread", t);
        }
      }
    });

  public void onRevoked() {
    log.info("Rebalance started");
    try {
      // 因为要重新选举，所以将之前的leader清空
      schemaRegistry.setMaster(null);
    } catch (SchemaRegistryException e) {
      // This shouldn't be possible with this implementation. The exceptions from setMaster come
      // from it calling nextRange in this class, but this implementation doesn't require doing
      // any IO, so the errors that can occur in the ZK implementation should not be possible here.
      log.error(
          "Error when updating master, we will not be able to forward requests to the master",
          e
      );
    }
  }
  
  // assignment为选举结果
  public void onAssigned(SchemaRegistryProtocol.Assignment assignment, int generation) {
    log.info("Finished rebalance with master election result: {}", assignment);
    try {
      switch (assignment.error()) {
        case SchemaRegistryProtocol.Assignment.NO_ERROR:
          if (assignment.masterIdentity() == null) {
            log.error(...);
          }
          // 记录分配结果
          schemaRegistry.setMaster(assignment.masterIdentity());
          joinedLatch.countDown();
          break;
        case SchemaRegistryProtocol.Assignment.DUPLICATE_URLS:
          throw new IllegalStateException(...);
        default:
          throw new IllegalStateException(...);
      }
    } catch (SchemaRegistryException e) {
      ......
    }
  }
} 
```



基于 zookeeper 的方式，会更加简单，效率也更高。因为只有 leader 挂掉，zookeeper 才会触发重新选举。而基于 kafka 的方式，只要是有一个成员挂掉，不管它是不是 leader，都会触发重新选举。如果这个成员不是 leader，则会造成不必要的选举。

使用zookeeper方式的原理是，所有 Registry 服务都会监听一个临时节点，而只有 leader 才会占有这个节点。当 leader 挂掉之后，临时节点会消失。其余的服务发现临时节点不存在，就会立即尝试重新创建，而只有一个服务能够创建成功，成为 leader。

```java
public class ZookeeperMasterElector implements MasterElector {
  // 选举使用的临时节点    
  private static final String MASTER_PATH = "/schema_registry_master";
    
  public void electMaster() throws
      SchemaRegistryStoreException, SchemaRegistryTimeoutException,
      SchemaRegistryInitializationException, IdGenerationException {
    SchemaRegistryIdentity masterIdentity = null;
    try {
      // 尝试在zookeeper中，创建临时节点
      zkUtils.createEphemeralPathExpectConflict(MASTER_PATH, myIdentityString,
                                                zkUtils.defaultAcls(MASTER_PATH));
      log.info("Successfully elected the new master: " + myIdentityString);
      masterIdentity = myIdentity;
      schemaRegistry.setMaster(masterIdentity);
    } catch (ZkNodeExistsException znee) {
      // 创建失败
      readCurrentMaster();
    }
  }    
    
    
  private class MasterChangeListener implements IZkDataListener {

    public MasterChangeListener() {
    }

    // 当数据被更改后，触发
    @Override
    public void handleDataChange(String dataPath, Object data) {
      
      try {
        if (isEligibleForMasterElection) {
          // 选举
          electMaster();
        } else {
          // 从节点中读取leader
          readCurrentMaster();
        }
      } catch (SchemaRegistryException e) {
        log.error("Error while reading the schema registry master", e);
      }
    }

    // leader 挂掉
    @Override
    public void handleDataDeleted(String dataPath) throws Exception {
      if (isEligibleForMasterElection) {
        // 如果允许选举，那么立即执行
        electMaster();
      } else {
        // 否则设置之前的leader为空
        schemaRegistry.setMaster(null);
      }
    }
  }

}    
```



## 参考资料

<https://docs.confluent.io/current/schema-registry/index.html>

<https://github.com/confluentinc/schema-registry>

http://avro.apache.org/docs/current/gettingstartedjava.html