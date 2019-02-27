---
title: Spark序列化与压缩原理
date: 2019-01-08 23:03:59
tags: spark, serializer
categories: spark
---

# Spark的序列化与压缩

spark是分布式的计算框架，其中涉及到了 rpc 的通信和中间数据的缓存。spark为了高效率的通信和减少数据存储空间，会把数据先序列化，然后处理。



## 序列化种类 ##

这篇文章讲的是spark 2.2，支持Java自带的序列化，还有KryoSerializer。KryoSerializer目前只能支持简单的数据类型，2.4对KryoSerializer的支持会更好。   

SerializerManager提供了getSerializer接口， 会自动选择选用哪种序列化方式， 默认为Java自带的序列化。对于使用Kryo序列化的条件比较苛刻，需要数据类型为原始类型或其对应的数组，并且支持autoPick（对应的block不是StreamBlock类型）。

从getSerializer的源码可以看到，目前支持kryo序列化的类型，有字符串类型，基本数据类型和其对应的数组类型这几种。

```scala
def getSerializer(ct: ClassTag[_], autoPick: Boolean): Serializer = {
  // 如果允许自动挑选，并且这种类型的数据支持kryo
  if (autoPick && canUseKryo(ct)) {
    kryoSerializer
  } else {
    // 否则返回默认序列化
    defaultSerializer
  }
}

// 字符串类型
private[this] val stringClassTag: ClassTag[String] = implicitly[ClassTag[String]]

private[this] val primitiveAndPrimitiveArrayClassTags: Set[ClassTag[_]] = {
  // 基本类型
  val primitiveClassTags = Set[ClassTag[_]](
      ClassTag.Boolean,
      ClassTag.Byte,
      ClassTag.Char,
      ClassTag.Double,
      ClassTag.Float,
      ClassTag.Int,
      ClassTag.Long,
      ClassTag.Null,
      ClassTag.Short
    )
    // 基本类型对应的数组
    val arrayClassTags = primitiveClassTags.map(_.wrap)
    // 合并类型
    primitiveClassTags ++ arrayClassTags
}

def canUseKryo(ct: ClassTag[_]): Boolean = {
  primitiveAndPrimitiveArrayClassTags.contains(ct) || ct == stringClassTag
}
```



## UML 类图

序列化的过程涉及到了多个类，这里使用了抽象工厂模式。SerializerInstance代表着抽象工厂，SerializationStream代表着序列化流，DeserializationStream代表着反序列化流。

Serializer也是SerializerInstance的工厂类，它通过newInstance实例化对应的SerializerInstance。

{% plantuml %}

@startuml spark-serializer

class Serializer  {

​	newInstance( )  :  SerializerInstance

}

class JavaSerializer

class KryoSerializer

class SerializerInstance {

​	serializeStream( ) :  SerializationStream

​	deserializeStream( ) : DeserializationStream

}

class SerializationStream

class DeserializationStream

class JavaSerializerInstance

class JavaSerializationStream

class JavaDeserializationStream

class KryoSerializerInstance

class KryoSerializationStream

class KryoDeserializationStream

Serializer <|--  JavaSerializer

Serializer <|--  KryoSerializer

JavaSerializer --> JavaSerializerInstance

KryoSerializer --> KryoSerializerInstance

Serializer --> SerializerInstance 

SerializerInstance --> SerializationStream

SerializerInstance --> DeserializationStream

SerializerInstance <|-- JavaSerializerInstance

SerializationStream <|-- JavaSerializationStream

DeserializationStream <|-- JavaDeserializationStream

JavaSerializerInstance --> JavaSerializationStream

JavaSerializerInstance --> JavaDeserializationStream

SerializerInstance <|-- KryoSerializerInstance

SerializationStream <|-- KryoSerializationStream

DeserializationStream <|-- KryoDeserializationStream

KryoSerializerInstance --> KryoSerializationStream

KryoSerializerInstance --> KryoDeserializationStream

@enduml

{% endplantuml %}



## 序列化相关的缓存流

spark序列化数据，提供了写入到缓存中和输出流。这里涉及到了下面几个类

ByteArrayOutputStream 将数据存储在字节数组里，并且可以自动扩充数组大小。

ByteBufferOutputStream 继承ByteArrayOutputStream， 提供了将数据转换为ByteBuffer的功能。

BufferedOutputStream 提供了缓存的作用，数据首先写入到BufferedOutputStream的缓存里，如果缓存满了，才会写入被装饰的底层流。

ChunkedByteBufferOutputStream 提供了多个小块ByteBuffer的数组，数据都会存在在各个ByteBuffer里面。当数据写满后，会新建ByteBuffer添加到数组里。

ChunkedByteBuffer 包含了ByteBuffer的数组， 提供了只读的功能。可以从ChunkedByteBufferOutputStream生成出来。

ChunkedByteBufferInputStream包装了ChunkedByteBuffer， 提供流式读取的接口。 



## Java序列化

JavaSerializerInstance负责java序列化的实现。它的serialize方法实现了序列化一个对象，它的serializeStream方法提供了序列化的流。

```scala
private[spark] class JavaSerializerInstance(
    counterReset: Int, extraDebugInfo: Boolean, defaultClassLoader: ClassLoader)
  extends SerializerInstance {
 
  // 它调用了serializeStream生成流，然后写入数据。
  override def serialize[T: ClassTag](t: T): ByteBuffer = {
    // 生成bytebuffer输出流
    val bos = new ByteBufferOutputStream()
    // 装饰序列化流
    val out = serializeStream(bos)
    // 写入数据
    out.writeObject(t)
    out.close()
    // 返回bytebuffer
    bos.toByteBuffer
  }
  
  // 返回JavaSerializationStream流
  override def serializeStream(s: OutputStream): SerializationStream = {
    new JavaSerializationStream(s, counterReset, extraDebugInfo)
  }
}
```



JavaSerializationStream作为流的装饰器，提供了序列化的功能。其实这里仅仅对ObjectOutputStream的封装，ObjectOutputStream是属于java库的类，通过它可以将数据序列化。不过ObjectOutputStream有个缺陷，当序列化的数据连续是同一个类型，ObjectOutputStream为了优化序列化的空间效率，会在内存中保存这些数据，这个有可能会导致内存溢出。所以JavaSerializationStream这里设置了定期每写入一定数目的数据，就会调用reset，避免这个问题。

```scala
private[spark] class JavaSerializationStream(
    out: OutputStream, counterReset: Int, extraDebugInfo: Boolean)
  extends SerializationStream {
      
  private val objOut = new ObjectOutputStream(out)
  // 自从上一次reset后，写入的数量
  private var counter = 0

  /**
   * Calling reset to avoid memory leak:
   * http://stackoverflow.com/questions/1281549/memory-leak-traps-in-the-java-standard-api
   * But only call it every 100th time to avoid bloated serialization streams (when
   * the stream 'resets' object class descriptions have to be re-written)
   */
  def writeObject[T: ClassTag](t: T): SerializationStream = {
    try {
      objOut.writeObject(t)
    } catch {
      case e: NotSerializableException if extraDebugInfo =>
        throw SerializationDebugger.improveException(t, e)
    }
    counter += 1
    // 每写入counterReset条数据，则调用reset
    if (counterReset > 0 && counter >= counterReset) {
      objOut.reset()
      counter = 0
    }
    this
  }

  def flush() { objOut.flush() }
  def close() { objOut.close() }
}
```



deserialize 方法提供了反序列化，反序列化涉及到了类的动态加载，这里可以指定ClassLoader。它生成JavaDeserializationStream，通过它解析数据。

```scala
private[spark] class JavaSerializerInstance(
    counterReset: Int, extraDebugInfo: Boolean, defaultClassLoader: ClassLoader)
  extends SerializerInstance {
  
  override def deserialize[T: ClassTag](bytes: ByteBuffer): T = {
    val bis = new ByteBufferInputStream(bytes)
    val in = deserializeStream(bis)
    in.readObject()
  }

  override def deserialize[T: ClassTag](bytes: ByteBuffer, loader: ClassLoader): T = {
    // 生成bytebuffer的输入流
    val bis = new ByteBufferInputStream(bytes)
   // 装饰反序列化流
    val in = deserializeStream(bis, loader)
    in.readObject()
  }

  override def deserializeStream(s: InputStream): DeserializationStream = {
    new JavaDeserializationStream(s, defaultClassLoader)
  }

  def deserializeStream(s: InputStream, loader: ClassLoader): DeserializationStream = {
    new JavaDeserializationStream(s, loader)
  }
  
```



JavaDeserializationStream的原理，它使用了ObjectInputStream类。ObjectInputStream类是属于java库的，它提供了反序列化的功能。这里实现了resolveClass方法，提供了指定ClassLoader来加载类。

```scala
private[spark] class JavaDeserializationStream(in: InputStream, loader: ClassLoader)
  extends DeserializationStream {
  
  private val objIn = new ObjectInputStream(in) {
    override def resolveClass(desc: ObjectStreamClass): Class[_] =
      try { 
        // scalastyle:off classforname
        Class.forName(desc.getName, false, loader)
        // scalastyle:on classforname
      } catch {
        case e: ClassNotFoundException =>
          JavaDeserializationStream.primitiveMappings.getOrElse(desc.getName, throw e)
      }
  }

  def readObject[T: ClassTag](): T = objIn.readObject().asInstanceOf[T]
  def close() { objIn.close() }
}
```



## Kryo 序列化

kryo的初始化，在KryoSerializer类的newKryo方法，Kryo预先注册需要序列化的类。

newKryoOutput方法会实例化KryoOutput, 作为数据存储的缓冲区。

```scala
class KryoSerializer(conf: SparkConf)
  extends org.apache.spark.serializer.Serializer {

  // 是否需要注册类，才能序列化对应的实例
  private val registrationRequired = conf.getBoolean("spark.kryo.registrationRequired", false)

  def newKryo(): Kryo = {
    // 这里通过EmptyScalaKryoInstantiator工厂，实例化Kryo
    val instantiator = new EmptyScalaKryoInstantiator
    val kryo = instantiator.newKryo()
    kryo.setRegistrationRequired(registrationRequired)
    // 如果没有ClassLoader，则使用当前线程的ClassLoader
    val oldClassLoader = Thread.currentThread.getContextClassLoader
    val classLoader = defaultClassLoader.getOrElse(Thread.currentThread.getContextClassLoader)

    // 向kryo注册类信息
    .........
    kryo.setClassLoader(classLoader)
    kryo
  }
      
  // 实例化KryoOutput， 使用默认的配置
  def newKryoOutput(): KryoOutput =
    if (useUnsafe) {
      new KryoUnsafeOutput(bufferSize, math.max(bufferSize, maxBufferSize))
    } else {
      new KryoOutput(bufferSize, math.max(bufferSize, maxBufferSize))
    }
  }

}
```



类的serialize方法实现了序列化一个对象，

```scala
class KryoSerializerInstance(ks: KryoSerializer, useUnsafe: Boolean)
  extends SerializerInstance {
  // 缓存的Kryo
  @Nullable private[this] var cachedKryo: Kryo = borrowKryo()
  
  // 调用KryoSerializer的newKryoOutput，实例化Kryo的缓存区
  private lazy val output = ks.newKryoOutput()

  override def serialize[T: ClassTag](t: T): ByteBuffer = {
    // 清除数据
    output.clear()
    // 如果已经有Kryo的实例，则直接返回。否则需要创建Kryo
    val kryo = borrowKryo()
    try {
      // 序列化数据
      kryo.writeClassAndObject(output, t)
    } catch {
      case e: KryoException if e.getMessage.startsWith("Buffer overflow") =>
        throw new SparkException(s"Kryo serialization failed: ${e.getMessage}. To avoid this, " +
          "increase spark.kryoserializer.buffer.max value.", e)
    } finally {
      releaseKryo(kryo)
    }
    // 返回序列化后的数据，注意output.toBytes会返回新的数组
    ByteBuffer.wrap(output.toBytes)
  }
      
      
  private lazy val output = ks.newKryoOutput()
      
}

```

 

serializeStream方法返回KryoSerializationStream

```scala
class KryoSerializationStream(
    serInstance: KryoSerializerInstance,
    outStream: OutputStream,
    useUnsafe: Boolean) extends SerializationStream {

  // 将outStream 装饰成 KryoOutput输出
  private[this] var output: KryoOutput =
    if (useUnsafe) new KryoUnsafeOutput(outStream) else new KryoOutput(outStream)

  private[this] var kryo: Kryo = serInstance.borrowKryo()

  override def writeObject[T: ClassTag](t: T): SerializationStream = {
    // 调用kryo的方法，序列化数据，写入outStream
    kryo.writeClassAndObject(output, t)
    this
  }
}
```



## SerializerManager接口 ##

SerializerManager统一了Java序列化和Kryo序列化的接口。我们只需要调用SerializerManager的方法，就可很方便的序列化数据。

### 序列化数据到内存

dataSerialize方法提供了将数据序列化，然后存到内存中。这里使用了ChunkedByteBufferOutputStream存储序列化的数据。使用了BufferedOutputStream在外层提供了缓存功能。在BufferedOutputStream之外，添加了压缩和序列化的功能。

```scala
/** Serializes into a chunked byte buffer. */
def dataSerialize[T: ClassTag](
    blockId: BlockId,
    values: Iterator[T]): ChunkedByteBuffer = {
  // 调用dataSerializeWithExplicitClassTag方法序列化
  dataSerializeWithExplicitClassTag(blockId, values, implicitly[ClassTag[T]])
}

/** Serializes into a chunked byte buffer. */
def dataSerializeWithExplicitClassTag(
    blockId: BlockId,
    values: Iterator[_],
    classTag: ClassTag[_]): ChunkedByteBuffer = {
  // 实例化ChunkedByteBufferOutputStream，保存序列化数据
  val bbos = new ChunkedByteBufferOutputStream(1024 * 1024 * 4, ByteBuffer.allocate)
  // 为bbos装饰为缓冲输出流
  val byteStream = new BufferedOutputStream(bbos)
  // StreamBlock类型的数据，不支持autoPick
  val autoPick = !blockId.isInstanceOf[StreamBlockId]
  // 获取序列化器
  val ser = getSerializer(classTag, autoPick).newInstance()
  // 调用wrapForCompression， 添加压缩流装饰器。
  // 然后添加序列化流装饰器
  ser.serializeStream(wrapForCompression(blockId, byteStream)).writeAll(values).close()
  // 返回序列化数据ChunkedByteBuffer
  bbos.toChunkedByteBuffer
}
```



### 序列化数据到流

序列化数据写入流，dataSerializeStream方法实现了装饰底层的流，并且将数据写入流中。

```scala
def dataSerializeStream[T: ClassTag](
    blockId: BlockId,
    outputStream: OutputStream,
    values: Iterator[T]): Unit = {
  // 实例化缓冲输出流
  val byteStream = new BufferedOutputStream(outputStream)
  // StreamBlock类型的数据，不支持autoPick
  val autoPick = !blockId.isInstanceOf[StreamBlockId]
  // 获取序列化器
  val ser = getSerializer(implicitly[ClassTag[T]], autoPick).newInstance()
  // 调用wrapForCompression， 添加压缩流装饰器。
  // 然后添加序列化流装饰器
  ser.serializeStream(wrapForCompression(blockId, byteStream)).writeAll(values).close()
}
```



## 数据压缩

当指定了spark.io.compression.codec配置的值后，spark会选择对应的压缩方式。

目前压缩方式支持三种方式， lz4， lzf， snappy。

压缩支持输入流和输出流，由CompressionCodec接口表示。

```scala
trait CompressionCodec {

  def compressedOutputStream(s: OutputStream): OutputStream

  def compressedInputStream(s: InputStream): InputStream
}
```

每种压缩方式都会实现这个接口。以lz4为例，

```scala
class LZ4CompressionCodec(conf: SparkConf) extends CompressionCodec {

  override def compressedOutputStream(s: OutputStream): OutputStream = {
    val blockSize = conf.getSizeAsBytes("spark.io.compression.lz4.blockSize", "32k").toInt
    // 返回LZ4BlockOutputStream包装的输出流
    new LZ4BlockOutputStream(s, blockSize)
  }

  // 返回LZ4BlockInputStream包装的输入流
  override def compressedInputStream(s: InputStream): InputStream = new LZ4BlockInputStream(s)
}
```



当序列化数据的时候，会根据存储Block的类型，判断是否需要压缩

```scala
private[spark] class SerializerManager(
 
  // Broadcast类型的数据是否压缩
  private[this] val compressBroadcast = conf.getBoolean("spark.broadcast.compress", true)
  // Shuffle类型的数据是否压缩
  private[this] val compressShuffle = conf.getBoolean("spark.shuffle.compress", true)
  // RDD类型的数据是否压缩 
  private[this] val compressRdds = conf.getBoolean("spark.rdd.compress", false)
  // 存储在磁盘的shuffle类型的数据，是否压缩
  private[this] val compressShuffleSpill = conf.getBoolean("spark.shuffle.spill.compress", true)

  // 根据数据的类型，判断是否配置中允许压缩
  private def shouldCompress(blockId: BlockId): Boolean = {
    blockId match {
      case _: ShuffleBlockId => compressShuffle
      case _: BroadcastBlockId => compressBroadcast
      case _: RDDBlockId => compressRdds
      case _: TempLocalBlockId => compressShuffleSpill
      case _: TempShuffleBlockId => compressShuffle
      case _ => false
    }
  }
}
```

