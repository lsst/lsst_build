#!/bin/bash

# Generates a YAML file in the format expected by lsst-build --repos=<yaml>
# from a directory populated git clones

# oneliner to generate repo list from current lsstsw/lsst_build "build" dir
repos=($(find ~lsstsw/build -maxdepth 2 -type d -name .git -exec grep url {}/config \; | cut -d' ' -f3))

echo "---"
for r in "${repos[@]}"; do
  basename=${r##*/}
  humanname=${basename%%.git}
  clonedir=repos/${humanname}
  url=$(echo $r | perl -p -e 's|github.com/LSST|github.com/lsst|')

  echo "$humanname: $url"
done
