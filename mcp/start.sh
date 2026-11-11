../bin/boundary-mcp-server --addr 127.0.0.1:8080

# Start with toolsets/keywords
# ../bin/boundary-mcp-server --addr 127.0.0.1:8080 --toolsets all

# Hand-picked tools, bypassing the groups entirely.
# ../bin/boundary-mcp-server --addr 127.0.0.1:8080 \
#   --tools list_targets,read_target,create_target,update_target

#list tools
./bin/boundary-mcp-server list-tools