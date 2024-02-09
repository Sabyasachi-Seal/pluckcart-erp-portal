### Installing ERPNext on Ubuntu 22.04
### Prerequisites:

Updated Ubuntu 22.04
A user with sudo privileges
Python 3.10+
Node.js 16
Hardware Requirements:

2GB RAM
20GB Hard Disk

Server Settings:

Update and Upgrade Packages

sudo apt-get update -y
sudo apt-get upgrade -y


### Install GIT

sudo apt-get install git

### Install Python

ERPNext version 14 requires Python version 3.10+. This is what we will install in this step.

sudo apt-get install python3-dev python3.10-dev python3-setuptools python3-pip python3-distutils
Install Python Virtual Environment

A virtual environment helps in managing the dependencies for one software at one place, without having to interfere with other sections in the computer or server in which the software is running.

sudo apt-get install python3.10-venv

### Software Properties Common will help in repository management.

sudo apt-get install software-properties-common

### Install MariaDB

ERPNext is built to naively run on MariaDB. The team is working to have the same working on PostgreSQL, but this is not ready yet.

sudo apt install mariadb-server mariadb-client

### Install Redis Server

sudo apt-get install redis-server

### Install other packages

ERPNext functionality also relies on other packages we will install in this step. These will load fonts, PDFs, and other resources to our instance.

sudo apt-get install xvfb libfontconfig wkhtmltopdf
sudo apt-get install libmysqlclient-dev

### Configure MYSQL Server

sudo mysql_secure_installation

When you run this command, the server will show the following prompts. Please follow the steps as shown below to complete the setup correctly.

Enter current password for root: (Enter your SSH root user password)
Switch to unix_socket authentication [Y/n]: Y
Change the root password? [Y/n]: Y
It will ask you to set new MySQL root password at this step. This can be different from the SSH root user password.
Remove anonymous users? [Y/n] Y
Disallow root login remotely? [Y/n]: N
This is set as N because we might want to access the database from a remote server for using business analytics software like Metabase / PowerBI / Tableau, etc.
Remove test database and access to it? [Y/n]: Y
Reload privilege tables now? [Y/n]: Y
Edit MYSQL default config file

sudo nano /etc/mysql/my.cnf

### Add the following block of code exactly as is:

[mysqld]
character-set-client-handshake = FALSE
character-set-server = utf8mb4
collation-server = utf8mb4_unicode_ci

[mysql]
default-character-set = utf8mb4
Restart the MYSQL Server

sudo service mysql restart

### Install CURL

sudo apt install curl

### Install Node

curl -sL https://deb.nodesource.com/setup_16.x | bash -
sudo apt-get install -y nodejs

### Install NPM

sudo apt-get install npm

### Install Yarn

sudo npm install -g yarn

### Install Frappe Bench

sudo pip3 install frappe-bench

### Initialize Frappe Bench

bench init --frappe-branch version-14 frappe-bench
cd frappe-bench

### Create a New Site

bench new-site [site-name]


### Install ERPNext and other Apps


bench get-app git@github.com:PluckCart/pluckcart-erpnext.git

### Install all the apps on our site
bench --site [site-name] install-app erpnext

### Set Default Site
bench use [site-name]

### Initialize Server

bench start
