#############################################################################
# Git support

import subprocess

class GitError:
	def __init__(self, returncode, cmd, output, stderr):
		self.returncode = returncode
		self.cmd = cmd
		self.output = output
		self.stderr = stderr

	def __str__(self):
		return "Command '%s' returned non-zero exit status %d." % (self.cmd, self.returncode)

class Git:
	def __init__(self, cwd=None):
		self.cwd = cwd

	@staticmethod
	def clone(*args):
		return Git()('clone', *args)

	def __call__(self, *args):
		# Run git with the given arguments, returning stdout.

		cmd = ('git',) + args
		#print "---> ", " ".join(cmd)

		process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.cwd)
		(stdout, stderr) = process.communicate()
		retcode = process.poll()
		if retcode:
			#print cmd
			#print stdout
			#print stderr
			raise GitError(retcode, cmd, stdout, stderr)

		return stdout.rstrip()

