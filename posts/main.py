import glob

filenames = glob.glob("*.md")
for src_name in filenames:
    PREFIX = "categories:"
    TAGS_PREFIX = "tags:"

    with open(src_name) as f:
        lines = f.readlines()


    new_lines = []
    for line in lines:
        line = line.decode('utf-8')
        new_line = line
        if line.startswith(PREFIX):
            category = line[len(PREFIX) : ].strip()
            print(category)
            new_line = PREFIX + " [" + category +  "]" + "\n"
            print(new_line)
        if line.startswith(TAGS_PREFIX):
            tags = line[len(TAGS_PREFIX) : ].strip()
            new_line = TAGS_PREFIX + " [" + tags + "]" + "\n"
        new_lines.append(new_line)

    with open(src_name, 'w') as f:
        data = u"".join(new_lines)
        f.write(data.encode('utf-8'))

