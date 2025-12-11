# 🎯 ACM Events Registration System  
A dynamic event registration platform built using **Flask + MongoDB**, featuring a powerful admin dashboard, customizable event settings, and smart registration forms that automatically adapt based on event configuration.

---

## 🚀 Features

### 🧑‍💼 Admin Panel
- Create, edit, and delete events  
- Enable/disable team name requirement  
- Set **minimum and maximum members** (supports 0)  
- View all registrations in a clean dashboard  
- Export registrations to Excel  
- Secure admin login with environment-based credentials  

### 📝 Smart Event Registration
- Users first choose an event  
- Registration form **automatically changes** based on the event:
  - If team name not required → hidden  
  - Member fields shown based on min/max limit  
  - Single attendee events fully supported  

### 🗄️ Database
- Stores events and registrations in MongoDB  
- Uses timestamps and unique IDs  
- Designed for scalability  

### 🧾 Export Capabilities
- Admin can download all registrations as an **Excel (.xlsx)** file  

### 🌐 Deployment Ready  
- Fully optimized for **Render**  
- Uses environment variables instead of leaking secrets  
- No SQLite — everything stored in MongoDB permanently  

---
