# A layer on the base image: Nullius writes its FROM. Its one file is its whole build context.
COPY env/marker.txt /opt/licensed-tool/marker.txt
