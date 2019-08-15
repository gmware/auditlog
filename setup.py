from distutils.core import setup
from os import path


this_directory = path.abspath(path.dirname(__file__))
with open(path.join(this_directory, 'README.md')) as f:
    long_description = f.read()


setup(
    name='auditlog',
    version='0.0.2',
    packages=['auditlog', 'auditlog.migrations', 'auditlog.management', 'auditlog.management.commands'],
    package_dir={'': 'src'},
    url='https://github.com/gmware/auditlog',
    license='MIT',
    author='Gmware',
    description='Based on django-auditlog with additional features for auditing logs.',
    long_description=long_description,
    long_description_content_type='text/markdown',
    install_requires=[
        'django-jsonfield>=1.0.0',
        'python-dateutil==2.6.0'
    ],
    zip_safe=False,
    classifiers=[
        'Programming Language :: Python :: 2',
        'Programming Language :: Python :: 2.7',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.4',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'License :: OSI Approved :: MIT License',
    ],        
)
