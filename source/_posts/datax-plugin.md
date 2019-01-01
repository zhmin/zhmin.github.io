---
title: Datax 插件加载原理
date: 2018-12-27 22:30:47
tags: data, plugin
---

# Datax 插件加载原理



## 插件类型

Datax有好几种类型的插件，每个插件都有不同的作用。

- reader， 读插件。Reader就是属于这种类型的
- writer， 写插件。Writer就是属于这种类型的
- transformer， 目前还未知
- handler， 主要用于任务执行前的准备工作和完成的收尾工作。

插件类型由PluginType枚举表示

```java
public enum PluginType {
	READER("reader"), TRANSFORMER("transformer"), WRITER("writer"), HANDLER("handler");
}
```

根据运行类型，又可以分为Job级别的插件和Task级别的插件。uml如下图所示

![](https://github.com/zhmin/blog/blob/datax/datax/images/plugin-uml.png?raw=true)



## 插件配置读取

ConfigParser首先会读取配置文件，提取需要使用的reader，writer，prehandler 和 posthandler的名称。

```java
    public static Configuration parse(final String jobPath) {
        Configuration configuration = ConfigParser.parseJobConfig(jobPath);
        // 合并 conf/core.json文件的配置, false 表示不覆盖原有的配置
        configuration.merge(
                //CoreConstant.DATAX_CONF_PATH的值为conf/core.json
                ConfigParser.parseCoreConfig
                (CoreConstant.DATAX_CONF_PATH),
                false);
        // 获取job.content列表的第一个reader
        String readerPluginName = configuration.getString(
                //CoreConstant.DATAX_JOB_CONTENT_READER_NAME的值为job.content[0].reader.name
                CoreConstant.DATAX_JOB_CONTENT_READER_NAME);
        // 获取job.content列表的第一个writer
        String writerPluginName = configuration.getString(
                //CoreConstant.DATAX_JOB_CONTENT_WRITER_NAME的值为job.content[0].writer.name
                CoreConstant.DATAX_JOB_CONTENT_WRITER_NAME);
        // 读取job.preHandler.pluginName
        String preHandlerName = configuration.getString(
                //CoreConstant.DATAX_JOB_PREHANDLER_PLUGINNAME的值为job.preHandler.pluginName
                CoreConstant.DATAX_JOB_PREHANDLER_PLUGINNAME);
        // 读取job.postHandler.pluginName
        String postHandlerName = configuration.getString(
                //CoreConstant.DATAX_JOB_POSTHANDLER_PLUGINNAME的值为job.postHandler.pluginName
                CoreConstant.DATAX_JOB_POSTHANDLER_PLUGINNAME);

        Set<String> pluginList = new HashSet<String>();
        pluginList.add(readerPluginName);
        pluginList.add(writerPluginName);
        ......
        // 调用parsePluginConfig生成plugin的配置，然后合并
        configuration.merge(parsePluginConfig(new ArrayList<String>(pluginList)), false);
        ......
        return configuration;
    }
```

提取完插件名称后，会去reader目录和writer目录，寻找插件的位置。目前Datax只支持reader和writer插件，因为它只从这两个目录中寻找。如果想自己扩展其他类型插件的话，比如handler类型的， 需要修改parsePluginConfig的代码。每个插件目录会有一个重要的配置文件 plugin.json ，它定义了插件的名称和对应的类，在LoadUtils类加载插件的时候会使用到。

```java
    public static Configuration parsePluginConfig(List<String> wantPluginNames) {
        Configuration configuration = Configuration.newDefault();
        ......
        // 遍历plugin.reader目录下的文件夹
        for (final String each : ConfigParser
                .getDirAsList(CoreConstant.DATAX_PLUGIN_READER_HOME)) {
            // 调用 parseOnePluginConfig解析单个plugin配置
            Configuration eachReaderConfig = ConfigParser.parseOnePluginConfig(each, "reader", replicaCheckPluginSet, wantPluginNames);
            if(eachReaderConfig!=null) {
                configuration.merge(eachReaderConfig, true);
                complete += 1;
            }
        }

        // 遍历plugin.writer目录下的文件夹
        for (final String each : ConfigParser
                .getDirAsList(CoreConstant.DATAX_PLUGIN_WRITER_HOME)) {
            // 调用 parseOnePluginConfig解析单个plugin配置
            Configuration eachWriterConfig = ConfigParser.parseOnePluginConfig(each, "writer", replicaCheckPluginSet, wantPluginNames);
            if(eachWriterConfig!=null) {
                configuration.merge(eachWriterConfig, true);
                complete += 1;
            }
        }
        
        ......

        return configuration;
    }

	// 读取plugin目录下的plugin.json 文件
	public static Configuration parseOnePluginConfig(final String path, final String type, Set<String> pluginSet, List<String> wantPluginNames) {
        String filePath = path + File.separator + "plugin.json";
        Configuration configuration = Configuration.from(new File(filePath));

        String pluginPath = configuration.getString("path");
        String pluginName = configuration.getString("name");
        if(!pluginSet.contains(pluginName)) {
            pluginSet.add(pluginName);
        } else {
            ......
        }

        //不是想要的插件，就不生成配置，直接返回
        if (wantPluginNames != null && wantPluginNames.size() > 0 && !wantPluginNames.contains(pluginName)) {
            return null;
        }

        // plugin.json的path路径，是指插件的jar包。如果没有指定，则默认为和plugin.json文件在同一个目录下
        boolean isDefaultPath = StringUtils.isBlank(pluginPath);
        if (isDefaultPath) {
            configuration.set("path", path);
        }

        Configuration result = Configuration.newDefault();
        // 最后保存在puligin.{type}.{pluginName}路径下
        result.set(
                String.format("plugin.%s.%s", type, pluginName),
                configuration.getInternal());

        return result;
    }

```

 

## 动态加载插件

插件的加载都是使用ClassLoader动态加载。 为了避免类的冲突，对于每个插件的加载，对应着独立的加载器。加载器由JarLoader实现，插件的加载接口由LoadUtil类负责。当要加载一个插件时，需要实例化一个JarLoader，然后切换thread class loader之后，才加载插件。

### JarLoader 类

JarLoader继承URLClassLoader，扩充了可以加载目录的功能。可以从指定的目录下，把传入的路径、及其子路径、以及路径中的jar文件加入到class path。

```java
public class JarLoader extends URLClassLoader {
    public JarLoader(String[] paths) {
        this(paths, JarLoader.class.getClassLoader());
    }

    public JarLoader(String[] paths, ClassLoader parent) {
        // 调用getURLS，获取所有的jar包路径
        super(getURLs(paths), parent);
    }

    // 获取所有的jar包
    private static URL[] getURLs(String[] paths) {
        // 获取包括子目录的所有目录路径
        List<String> dirs = new ArrayList<String>();
        for (String path : paths) {
            dirs.add(path);
            // 获取path目录和其子目录的所有目录路径
            JarLoader.collectDirs(path, dirs);
        }
        // 遍历目录，获取jar包的路径
        List<URL> urls = new ArrayList<URL>();
        for (String path : dirs) {
            urls.addAll(doGetURLs(path));
        }

        return urls.toArray(new URL[0]);
    }

    // 递归的方式，获取所有目录
    private static void collectDirs(String path, List<String> collector) {
        // path为空，终止
        if (null == path || StringUtils.isBlank(path)) {
            return;
        }

        // path不为目录，终止
        File current = new File(path);
        if (!current.exists() || !current.isDirectory()) {
            return;
        }

        // 遍历完子文件，终止
        for (File child : current.listFiles()) {
            if (!child.isDirectory()) {
                continue;
            }

            collector.add(child.getAbsolutePath());
            collectDirs(child.getAbsolutePath(), collector);
        }
    }    

    private static List<URL> doGetURLs(final String path) {
        
        File jarPath = new File(path);
		// 只寻找文件以.jar结尾的文件
        FileFilter jarFilter = new FileFilter() {
            @Override
            public boolean accept(File pathname) {
                return pathname.getName().endsWith(".jar");
            }
        };

		
        File[] allJars = new File(path).listFiles(jarFilter);
        List<URL> jarURLs = new ArrayList<URL>(allJars.length);

        for (int i = 0; i < allJars.length; i++) {
            try {
                jarURLs.add(allJars[i].toURI().toURL());
            } catch (Exception e) {
                throw DataXException.asDataXException(
                        FrameworkErrorCode.PLUGIN_INIT_ERROR,
                        "系统加载jar包出错", e);
            }
        }

        return jarURLs;
    }
}
```



### LoadUtil 类

LoadUtil管理着插件的加载器，调用getJarLoader返回插件对应的加载器。

```java
public class LoadUtil {
    
    // 加载器的HashMap, Key由插件类型和名称决定, 格式为plugin.{pulginType}.{pluginName}
    private static Map<String, JarLoader> jarLoaderCenter = new HashMap<String, JarLoader>();

	public static synchronized JarLoader getJarLoader(PluginType pluginType, String pluginName) {
        Configuration pluginConf = getPluginConf(pluginType, pluginName);

        JarLoader jarLoader = jarLoaderCenter.get(generatePluginKey(pluginType,
                pluginName));
        if (null == jarLoader) {
            // 构建加载器JarLoader
            // 获取jar所在的目录
            String pluginPath = pluginConf.getString("path");
            jarLoader = new JarLoader(new String[]{pluginPath});
            //添加到HashMap中
            jarLoaderCenter.put(generatePluginKey(pluginType, pluginName),
                    jarLoader);
        }

        return jarLoader;
    }

    private static final String pluginTypeNameFormat = "plugin.%s.%s";
	
    // 生成HashMpa的key值
    private static String generatePluginKey(PluginType pluginType,
                                            String pluginName) {
        return String.format(pluginTypeNameFormat, pluginType.toString(),
                pluginName);
    }
```

当获取类加载器，就可以调用LoadUtil来加载插件。LoadUtil提供了 loadJobPlugin 和  loadTaskPlugin 两个接口，加载Job  和 Task 的两种插件。

```java
// 加载Job类型的Plugin
public static AbstractJobPlugin loadJobPlugin(PluginType pluginType, String pluginName) {
        // 调用loadPluginClass方法，加载插件对应的class
        Class<? extends AbstractPlugin> clazz = LoadUtil.loadPluginClass(pluginType, pluginName, ContainerType.Job);

        // 实例化Plugin，转换为AbstractJobPlugin
        AbstractJobPlugin jobPlugin = (AbstractJobPlugin) clazz.newInstance();
        // 设置Job的配置,路径为plugin.{pluginType}.{pluginName}
        jobPlugin.setPluginConf(getPluginConf(pluginType, pluginName));
        return jobPlugin;

    }

// 加载Task类型的Plugin
public static AbstractTaskPlugin loadTaskPlugin(PluginType pluginType, String pluginName) {
        // 调用loadPluginClass方法，加载插件对应的class
        Class<? extends AbstractPlugin> clazz = LoadUtil.loadPluginClass(pluginType, pluginName, ContainerType.Task);
        // 实例化Plugin，转换为AbstractTaskPlugin
        AbstractTaskPlugin taskPlugin = (AbstracTasktTaskPlugin) clazz.newInstance();
        // 设置Task的配置,路径为plugin.{pluginType}.{pluginName}
        taskPlugin.setPluginConf(getPluginConf(pluginType, pluginName));
    }

// 加载插件类
// pluginType 代表插件类型
// pluginName 代表插件名称
// pluginRunType 代表着运行类型，Job或者Task
private static synchronized Class<? extends AbstractPlugin> loadPluginClass(
    PluginType pluginType, String pluginName,
    ContainerType pluginRunType) {
    // 获取插件配置
    Configuration pluginConf = getPluginConf(pluginType, pluginName);
    // 获取插件对应的ClassLoader
    JarLoader jarLoader = LoadUtil.getJarLoader(pluginType, pluginName);
    try {
        // 加载插件的class
        return (Class<? extends AbstractPlugin>) jarLoader
            .loadClass(pluginConf.getString("class") + "$"
                       + pluginRunType.value());
    } catch (Exception e) {
        throw DataXException.asDataXException(FrameworkErrorCode.RUNTIME_ERROR, e);
    }
}
```



## 切换类加载器

ClassLoaderSwapper类，提供了比较方便的切换接口。

```java
// 实例化
ClassLoaderSwapper classLoaderSwapper = ClassLoaderSwapper.newCurrentThreadClassLoaderSwapper();

ClassLoader classLoader1 = new URLClassLoader();
// 切换加载器classLoader1
classLoaderSwapper.setCurrentThreadClassLoader(classLoader1);
Class<? extends MyClass> myClass = classLoader1.loadClass("MyClass");
// 切回加载器
classLoaderSwapper.restoreCurrentThreadClassLoader();

```

ClassLoaderSwapper的源码比较简单， 它有一个属性storeClassLoader， 用于保存着切换之前的ClassLoader。

```java
public final class ClassLoaderSwapper {
    
    // 保存切换之前的加载器
    private ClassLoader storeClassLoader = null;

    public ClassLoader setCurrentThreadClassLoader(ClassLoader classLoader) {
        // 保存切换前的加载器
        this.storeClassLoader = Thread.currentThread().getContextClassLoader();
        // 切换加载器到classLoader
        Thread.currentThread().setContextClassLoader(classLoader);
        return this.storeClassLoader;
    }


    public ClassLoader restoreCurrentThreadClassLoader() {
        
        ClassLoader classLoader = Thread.currentThread()
                .getContextClassLoader();
        // 切换到原来的加载器
        Thread.currentThread().setContextClassLoader(this.storeClassLoader);
        // 返回切换之前的类加载器
        return classLoader;
    }
}

```

