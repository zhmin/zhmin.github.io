---
title: Datax 任务执行流程
date: 2018-12-27 16:33:43
tags: datax
---

# Datax 任务执行流程



## 加载配置

Datax启动是从Engine类开始的。Engine会读取配置文件，并且初始化和运行JobContainer。

```java
public class Engine {
	public static void entry(final String[] args) throws Throwable {
    	// .....
        Configuration configuration = ConfigParser.parse(jobPath);
        Engine engine = new Engine();
        engine.start(configuration);
	}
    
    public void start(Configuration allConf) {
        // ......
        container = new JobContainer(allConf);
        container.start();
    }
}
```



## JobContainer原理

JobContaier的源码涉及到插件的动态加载，可以参考此篇博客。

首先看看JobContainer的start方法，它将任务的执行分为多个阶段。

```java
public class JobContainer extends AbstractContainer {
    public void start() {
        LOG.debug("jobContainer starts to do preHandle ...");
        this.preHandle();
        LOG.debug("jobContainer starts to do init ...");
        this.init();
        LOG.info("jobContainer starts to do prepare ...");
        this.prepare();
        LOG.info("jobContainer starts to do split ...");
        this.totalStage = this.split();
        LOG.info("jobContainer starts to do schedule ...");
        this.schedule();
        LOG.debug("jobContainer starts to do post ...");
        this.post();
        LOG.debug("jobContainer starts to do postHandle ...");
        this.postHandle();
        LOG.info("DataX jobId [{}] completed successfully.", this.jobId);
    }
}
        
```



### 加载 和运行 Handler的初始化函数

这里会根据配置，加载指定的Handler类，并且执行它的preHandler回调函数。相关的配置如下：

- job.preHandler.pluginType， 插件类型
- job.preHandler.pluginName， 插件名称

```java
private void preHandle() {
    // 获取preHandler的类型，由job.preHandler.pluginType指定
    String handlerPluginTypeStr = this.configuration.getString(
            CoreConstant.DATAX_JOB_PREHANDLER_PLUGINTYPE);
    // 实例化PluginType
    PluginType handlerPluginType = PluginType.valueOf(handlerPluginTypeStr.toUpperCase());
	// 获取preHandler的名称
    String handlerPluginName = this.configuration.getString(
            CoreConstant.DATAX_JOB_PREHANDLER_PLUGINNAME);
    // 切换类加载器
    classLoaderSwapper.setCurrentThreadClassLoader(LoadUtil.getJarLoader(
            handlerPluginType, handlerPluginName));
	// 加载 Handler
    AbstractJobPlugin handler = LoadUtil.loadJobPlugin(
            handlerPluginType, handlerPluginName);
	// 初始化handler的jobPluginCollector， 通过它可以知道任务的详情
    JobPluginCollector jobPluginCollector = new DefaultJobPluginCollector(
            this.getContainerCommunicator());
    handler.setJobPluginCollector(jobPluginCollector);

    //调用handler的preHandler
    handler.preHandler(configuration);
    // 切回类加载器
    classLoaderSwapper.restoreCurrentThreadClassLoader();
}
```



### 加载Reader和Writer

init方法会将加载配置中指定的Reader和Writer，来完成数据的读取和写入。Reader的初始化和Writer相同，这里以Reader为例：

```java
private void init() {
    JobPluginCollector jobPluginCollector = new DefaultJobPluginCollector(this.getContainerCommunicator());
    //必须先Reader ，后Writer
    this.jobReader = this.initJobReader(jobPluginCollector);
    this.jobWriter = this.initJobWriter(jobPluginCollector);
}

// 加载Reader
private Reader.Job initJobReader(JobPluginCollector jobPluginCollector) {
    // 读取 Reader的名称
    this.readerPluginName = this.configuration.getString(
        CoreConstant.DATAX_JOB_CONTENT_READER_NAME);
    // 切换类加载器
    classLoaderSwapper.setCurrentThreadClassLoader(LoadUtil.getJarLoader(
        PluginType.READER, this.readerPluginName));
    // 加载Reader插件
    Reader.Job jobReader = (Reader.Job) LoadUtil.loadJobPlugin(
        PluginType.READER, this.readerPluginName);
    // 更新Reader的配置
    jobReader.setPluginJobConf(this.configuration.getConfiguration(
        CoreConstant.DATAX_JOB_CONTENT_READER_PARAMETER));
    jobReader.setPeerPluginJobConf(this.configuration.getConfiguration(
        CoreConstant.DATAX_JOB_CONTENT_WRITER_PARAMETER));
    // 初始化Reader的jobPluginCollector
    jobReader.setJobPluginCollector(jobPluginCollector);
    // 调用Reader的init回调函数
    jobReader.init();
    // 切回类加载器
    classLoaderSwapper.restoreCurrentThreadClassLoader();
    return jobReader;
}
    
```



### Reader和Writer的初始化回调函数

prepare方法会执行Reader和Writer的prepare回调函数。Reader和Writer相同，下面以Reader为例：

```java
private void prepare() {
	this.prepareJobReader();
    this.prepareJobWriter();
}
    
private void prepareJobReader() {
    // 切换类加载器
    classLoaderSwapper.setCurrentThreadClassLoader(LoadUtil.getJarLoader(
            PluginType.READER, this.readerPluginName));
    LOG.info(String.format("DataX Reader.Job [%s] do prepare work .",
            this.readerPluginName));
    // 调用jobReader的prepare方法
    this.jobReader.prepare();
    // 切回类加载器
    classLoaderSwapper.restoreCurrentThreadClassLoader();
}
```



### 切分任务

split方法会根据channel的数目，将整个job任务，划分成多个小的task。具体原理参见这篇博客。



### 分配和执行任务

schedule方法会将task发送给各个Channel执行。具体原理参见这篇博客。



### 调用Reader和Writer的完成回调函数

post会执行Reader和Writer的post回调函数。Reader和Writer相同，下面以Reader为例：

```java
private void post() {
    this.postJobWriter();
    this.postJobReader();
}

private void postJobReader() {
	// 切换类加载器
	classLoaderSwapper.setCurrentThreadClassLoader(LoadUtil.getJarLoader(
		PluginType.READER, this.readerPluginName));
    LOG.info("DataX Reader.Job [{}] do post work.", this.readerPluginName);
    // 调用jobReader的post方法
    this.jobReader.post();
    // 切回类加载器
    classLoaderSwapper.restoreCurrentThreadClassLoader();
}
```



### 加载 和运行 Handler的完成函数

这里会根据配置，加载指定的Handler类，并且执行它的postHandler回调函数。相关的配置如下：

- job.postHandler.pluginType， 插件类型
- job.postHandler.pluginName， 插件名称

```java
private void postHandle() {
    // 获取插件类型
	String handlerPluginTypeStr = this.configuration.getString(
		CoreConstant.DATAX_JOB_POSTHANDLER_PLUGINTYPE);
    PluginType handlerPluginType = PluginType.valueOf(handlerPluginTypeStr.toUpperCase());
    
    // 获取插件名称
    String handlerPluginName = this.configuration.getString(
        CoreConstant.DATAX_JOB_POSTHANDLER_PLUGINNAME);
    // 切换类加载器
    classLoaderSwapper.setCurrentThreadClassLoader(LoadUtil.getJarLoader(
        handlerPluginType, handlerPluginName));
    // 加载插件
    AbstractJobPlugin handler = LoadUtil.loadJobPlugin(
        handlerPluginType, handlerPluginName);
    
    // 配置jobPluginCollector， 通过它可以得到任务执行的详情
    JobPluginCollector jobPluginCollector = new DefaultJobPluginCollector(
        this.getContainerCommunicator());
    handler.setJobPluginCollector(jobPluginCollector);
    
    // 调用handler的postHandler函数
    handler.postHandler(configuration);
    // 切回类加载器
    classLoaderSwapper.restoreCurrentThreadClassLoader();
```



## 扩展 handler 插件

如果有个需求，需要将任务的完成情况，记录下来。这个时候需要自定义handler。

### 修改插件加载

首先需要修改读取插件配置的源码，因为从ConfigParser的源码可以看到，datax已经从配置文件中，提取了posthandler的插件名称，但是在寻找插件的时候，只是从reader和writer目录下去寻找，所以需要增加从handler目录的寻找。

```java
public class CoreConstant {
	public static String DATAX_PLUGIN_HANDLER_HOME = StringUtils.join(
			new String[] { DATAX_HOME, "plugin", "handler" }, File.separator);
}

public final class ConfigParser {
    public static Configuration parsePluginConfig(List<String> wantPluginNames) {
        .......
         for(final String each : ConfigParser
             	.getDirAsList(CoreConstant.DATAX_PLUGIN_HANDLER_HOME)) {
             Configuration eachHandlerConfig = ConfigParser.parseOnePluginConfig(each, 
                  		"handler", replicaCheckPluginSet, wantPluginNames);
             if (eachHandlerConfig != null) {
                 configuration.merge(eachHandlerConfig, true);
                 complete += 1;
             }
         }
    }
}
     
```



### 自定义Handler类

因为postHandler是属于Job运行类型的插件，所有必须在里面新建一个名称为Job的类，这个Job类必须继承AbstractJobPlugin。下面就是自定义插件的JobResultCollector的基本雏形

```java
public class JobResultCollector {

    private static final Logger LOG = LoggerFactory.getLogger(Collector.class);

    public static class Job extends AbstractJobPlugin {

        @Override
        public void init() {
            LOG.info("plugin Collector start init");
        }

        @Override
        public void destroy() {
            LOG.info("plugin Collector destroy");
        }

        @Override
        public void postHandler(Configuration jobConfiguration) {
            LOG.info("plugin Collector post handler");
        }
    }
}
```

### 增加结果数据读取功能

因为这个插件需要记录任务完成结果，所以需要了解下怎么获取统计数据。关于统计数据的原理，可以参见此篇文章

因为自定义Handler属于Job运行类型的插件，所以需要继承AbstractJobPlugin。AbstractJobPlugin有一个重要属性jobPluginCollector，通过它可以获得需要的数据。

```java
public abstract class AbstractJobPlugin extends AbstractPlugin {
    
    private JobPluginCollector jobPluginCollector;

    public JobPluginCollector getJobPluginCollector() {
        return jobPluginCollector;
    }
    
    public void setJobPluginCollector(
            JobPluginCollector jobPluginCollector) {
		this.jobPluginCollector = jobPluginCollector;
	}   
}
```



通过加载posthandler的源码中，可以看到jobPluginCollector变量的初始化，实质上是DefaultJobPluginCollector的实例。并且每个插件都有着独立的JobPluginCollector， 但这些JobPluginCollector都共享者同一个AbstractContainerCommunicator

```java
public class JobContainer extends AbstractContainer {

	private void postHandle() {
        ........
        // 加载和实例化handler
        AbstractJobPlugin handler = LoadUtil.loadJobPlugin(
                handlerPluginType, handlerPluginName);
		// 实例化DefaultJobPluginCollector
        JobPluginCollector jobPluginCollector = new DefaultJobPluginCollector(
                this.getContainerCommunicator());
        // 设置handler的jobPluginCollector
        handler.setJobPluginCollector(jobPluginCollector);
        ........
        handler.postHandler(configuration);
}
```

接下来看看DefaultJobPluginCollector的源码，可以看到它只提供了关于返回消息的方法，却没有提供别的数据的访问。感觉像是阿里这边的源码并没有开放完全。所以如果想访问到其他的数据，就需要修改源码了。

```java
public final class DefaultJobPluginCollector implements JobPluginCollector {
    private AbstractContainerCommunicator jobCollector;

    public DefaultJobPluginCollector(AbstractContainerCommunicator containerCollector) {
        this.jobCollector = containerCollector;
    }

    @Override
    public Map<String, List<String>> getMessage() {
        Communication totalCommunication = this.jobCollector.collect();
        return totalCommunication.getMessage();
    }

    @Override
    public List<String> getMessage(String key) {
        Communication totalCommunication = this.jobCollector.collect();
        return totalCommunication.getMessage(key);
    }
}
```

原本我是想增加一个方法返回Communication。这样根据返回的Communication，可以查看里面任何的数据。因为DefaultJobPluginCollector实现JobPluginCollector接口，所以也需要加上。 代码如下：

```java
public final class DefaultJobPluginCollector implements JobPluginCollector {
    
    public Communication getCommunication() {
        return this.jobCollector.collect();
    }
}

public interface JobPluginCollector extends PluginCollector {
    Communication getCommunication();
}
```

但是行不通，因为DefaultJobPluginCollector是属于datax-core模块的类，JobPluginCollector属于datax-common模块的类。datax-core是依赖于datax-common的。而Communication是属于datax-core模块的类，如果在JobPluginCollector引用Communication，就会产生循环依赖。

所以增加方法必须考虑依赖关系，因为Communication的数据类型比较简单，这样接口只需要返回简单的数据类型即可

```java
public final class DefaultJobPluginCollector implements JobPluginCollector {
    
    @Override
    public Map<String, Number> getCounter() {
        return this.jobCollector.collect().getCounter();
    }
}

public interface JobPluginCollector extends PluginCollector {
    Map<String, Number> getCounter();
}
```



现在解决了读取数据的需求了，接下来实现JobResultCollector的postHandler方法。可以结合CommunicationTool里面的变量，获取对应的数据。

```java
@Override
public void postHandler(Configuration jobConfiguration) {
    LOG.info("plugin Collector post handler");
    Map<String, Number> counter = getJobPluginCollector().getCounter();
    long successReadRecords = getLongCounter(counter, CommunicationTool.READ_SUCCEED_RECORDS);
    long failedReadRecords = getLongCounter(counter, CommunicationTool.READ_FAILED_RECORDS);
    long totalReadRecords = successReadRecords - failedReadRecords;
    LOG.info("total records : " + totalReadRecords);
}

private long getLongCounter(Map<String, Number> counter, String key) {
    Number value = counter.get(key);
    return value == null ? 0 : value.longValue();
}
```

