from distutils.core import setup
import io

try:
    with io.open('README.md', encoding="utf-8") as f:
        long_description = f.read()
except IOError:
    long_description = ''

setup(
    name='auditlog',
    version='0.0.5',
    packages=['auditlog', 'auditlog.migrations', 'auditlog.management', 'auditlog.management.commands'],
    package_dir={'': 'src'},
    url='https://github.com/gmware/auditlog',
    license='MIT',
    author='Gmware',
    description='Based on django-auditlog with additional features for auditing logs.',
    long_description=long_description,
    long_description_content_type='text/x-rst',
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
